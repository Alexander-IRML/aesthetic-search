# CSD CPU Inference Optimization

Status: measured and production-safe changes selected on 2026-09-05

## Finding

Warm image-search latency is dominated by the canonical CSD ViT-L/14
backbone, not image preprocessing, vector normalization, Qdrant, or the style
projection. The serving model has 303,966,208 parameters and occupies about
1.16 GiB as FP32 weights. Keeping the exact model is necessary until a student
encoder is trained against retrieval quality labels.

The local profile used the pinned production checkpoint on an Intel i7-1360P
under WSL. One-image warm runs with one CPU thread produced this mean breakdown:

| CSD stage | Mean |
| --- | ---: |
| Processor resize/normalize | 4.19 ms |
| Tensor transfer | 0.01 ms |
| ViT-L/14 backbone | 2,112.87 ms |
| Style projection + L2 normalization | 0.35 ms |
| NumPy output | 0.05 ms |
| Total | 2,117.52 ms |

PyTorch operator profiling attributed 82.76% of self CPU time to `aten::addmm`
across 96 calls. CPU flash attention was 5.30%; copies, activation multiply,
and sigmoid accounted for most of the remainder. Dense transformer matrix
multiplication is therefore the optimization target.

## Thread Scaling

Warm median latency from the same pinned FP32 model was:

| CPU threads | Median | Speedup vs. one thread |
| ---: | ---: | ---: |
| 1 | 1,688.49 ms | 1.00x |
| 2 | 900.67 ms | 1.87x |
| 4 | 567.16 ms | 2.98x |

The public deployment now uses one scale-to-zero Container with 2 vCPU, 6 GiB
memory, and `OMP_NUM_THREADS=2` / `MKL_NUM_THREADS=2`. Six GiB is the minimum
memory allocation for a custom two-vCPU Cloudflare Container. The one-instance
and one-inference-owner ceilings remain unchanged.

## Runtime Experiments

The experiments below used four row-aligned image vectors and compared each
candidate runtime directly with canonical PyTorch FP32 output. They did not
alter the corpus or active Qdrant alias.

| Runtime | Median latency | Artifact size | Min / mean cosine vs. FP32 | Decision |
| --- | ---: | ---: | ---: | --- |
| PyTorch FP32, 1 thread | 1,688 ms | existing checkpoint | 1.0 / 1.0 | Canonical |
| ONNX FP32, 1 thread | 1,684 ms | 1.216 GB | ~1.0 / ~1.0 | Reject: negligible speedup and large artifact |
| ONNX FP32, 2 threads | 873 ms | 1.216 GB | ~1.0 / ~1.0 | Reject: no benefit over PyTorch thread scaling |
| PyTorch dynamic INT8 | 1,455 ms | transient | 0.638 / 0.756 | Reject: severe embedding drift |
| ONNX dynamic per-channel INT8, 1 thread | 569 ms | 308 MB | 0.884 / 0.911 | Reject: fails quality gate |
| ONNX dynamic per-channel INT8, 2 threads | 320 ms | 308 MB | 0.884 / 0.911 | Reject: fails quality gate |

No ONNX or quantized model artifact is retained in the repository or deployed
image. OpenVINO remains an unselected experiment until it has a reproducible
latency result and passes the same retrieval-quality gate; it is not part of
the production dependency set.

## Reproduce

The benchmark command defaults to deterministic generated images, so it does
not need or persist private corpus media:

```bash
.venv/bin/python -m artsearch.embed.csd_benchmark \
  --config config/config.yaml \
  --threads 1 \
  --threads 2 \
  --warmup 1 \
  --repeats 3
```

Real, legally usable fixtures can be supplied with repeated `--path` options.
The report includes mean, p50, and p95 for every CSD stage and verifies that
thread-count outputs agree with the first canonical run.

Any alternative runtime must pass both vector gates before retrieval testing:

- minimum row-wise cosine similarity at least `0.995`
- maximum absolute component error at most `0.02`

Those gates only reject obviously incompatible encoders. Promotion still
requires top-k neighbor overlap and nDCG on the held-out retrieval judgment set.
A distilled or LoRA-trained baby encoder remains the next major optimization;
it must emit vectors compatible with the active CSD space or trigger a new
versioned corpus/index build.

## Live Trace Contract

Every successful API response now separates CSD preprocessing, tensor
transfer, backbone, projection, and output time. PostgreSQL query initialization
runs concurrently with image encoding. `telemetry_start_ms` records all of that
database work, while `telemetry_wait_ms` records only the residual join on the
response critical path. These overlapping fields must not be summed as a
waterfall.

## Deployment Validation

Cloudflare application version 4 was promoted with image
`csd-2vcpu-20260905`, 2 vCPU, and 6 GiB memory. A generated solid-color query
confirmed the new schema and returned ten results from the unchanged
`artworks_active` alias.

The first query on the new container revision spent 31.30 seconds starting the
container. Inside the API, one-time model initialization made CLIP 5.61 seconds
and CSD 1.81 seconds. Telemetry initialization took 5.44 seconds concurrently
and left only 0.016 ms of residual wait.

The immediately following warm query measured:

| Stage | Single-probe latency |
| --- | ---: |
| Browser request | 2,697 ms |
| Cloudflare edge | 2,595 ms |
| Python API | 2,512 ms |
| CLIP | 184 ms |
| CSD total | 1,689 ms |
| CSD backbone | 1,684 ms |
| Qdrant | 353 ms |
| PostgreSQL catalog | 283 ms |
| Telemetry DB work / residual wait | 514 ms / 0.008 ms |

The earlier warm trace was about 5,207 ms at the edge, 4,951 ms in the API,
and 3,438 ms in CSD. The new single probe is roughly 50% faster at each of those
boundaries. This is deployment validation, not a latency distribution; p50 and
p95 should be read from normal telemetry after enough version-4 searches.
