# ADR 0002: Replace DINO Retrieval Features With CSD

- Status: accepted
- Date: 2026-08-20
- Decision owners: ArtSearch
- Supersedes: DINO pooled and DINO patch retrieval in the active architecture

## Context

The prior retrieval stack stored one pooled DINOv2 vector plus a matrix of
DINOv2 patch tokens for every artwork. Pooled DINO was being asked to represent
style despite not being trained specifically for that task, while patch MaxSim
added substantial storage and a second exact reranking stage.

ArtSearch needs a compact style representation that remains independent from
CLIP's semantic/subject representation. Contrastive Style Descriptors (CSD)
were trained to separate visual style from image content and produce a single
normalized style descriptor per image.

## Decision

Use two current retrieval representations:

| Representation | Role | Dimension |
| --- | --- | ---: |
| CLIP ViT-B/32 image projection | semantic and subject recall | 512 |
| CSD ViT-L style head | artistic-style recall | 768 |

Run independent cosine retrieval for both representations. Union their recall
sets and order the union with reciprocal-rank fusion. Preserve the component
similarity and rank evidence in search results and human judgments.

Remove DINO pooled vectors, DINO patch matrices, patch diagnostics, and patch
MaxSim from the active embedding, SQLite, dashboard, and Qdrant contracts.
Legacy retrieval judgment fields and mode values remain parseable so historical
append-only evaluation data is not destroyed.

## Artifact Contract

The runtime artifact is intentionally pinned:

```text
repository: yuxi-liu-wired/CSD
revision: cd2dac3ad9398e42906127bc44988a6de47abe68
filename: csd_clip_model_pytorch.safetensors
sha256: 02d842ed2536e1f883fc832cd1566d0e6c569457979f878379e382e04b2bda84
processor: openai/clip-vit-large-patch14
processor revision: 32bd64288804d66eefd0ccbe215aa642df71cc41
```

The loader verifies the artifact SHA-256 before safe tensor loading, rebuilds
the authors' standard CLIP ViT-L/14 visual architecture locally, strictly
validates every backbone weight, validates a finite `(batch, 768)` style-head
output, and normalizes each vector. Model code is not executed from a moving
Hugging Face branch.

The chosen safetensors file is a third-party conversion of the authors'
checkpoint. Its model card warns that style/content separation may not exactly
match expectations, and the authors' repository notes a discrepancy between
the currently released weights and paper results. Therefore this is a strong
baseline, not a quality claim. Promotion depends on ArtSearch human judgments.

## Index Migration

The new physical Qdrant collection is `artworks_clip3_csd1_v1`, with named
vectors `clip_subject` and `csd_style`. The old DINO collection is incompatible
and must never be reused or relabeled. Build and verify the CSD collection,
then move the stable `artworks_active` alias atomically.

Existing SQLite databases migrate additively by adding CSD columns. Old DINO
columns can remain until a later offline database compaction; active queries no
longer read or write them.

## Consequences

- Per-artwork style storage becomes one 768-dimensional vector instead of a
  global vector plus a patch matrix.
- Retrieval and evaluation have three active modes: ensemble, CLIP subject,
  and CSD style.
- The system no longer has a dedicated local-detail retriever. A replacement
  should be introduced only after judged failure analysis identifies that need.
- CSD inference uses a ViT-L/14 backbone and may cost more compute per image
  than the former pooled encoder even though storage and serving are smaller.
- Historical DINO release notes remain historical and are not descriptions of
  the current runtime.

## References

- [Contrastive Style Descriptors paper](https://arxiv.org/abs/2404.01292)
- [Authors' CSD repository](https://github.com/learn2phoenix/CSD)
- [Authors' Hugging Face model](https://huggingface.co/tomg-group-umd/CSD-ViT-L)
- [Pinned TorchScript conversion](https://huggingface.co/yuxi-liu-wired/CSD)
