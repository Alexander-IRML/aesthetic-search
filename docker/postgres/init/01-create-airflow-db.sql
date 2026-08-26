SELECT 'CREATE DATABASE airflow OWNER artsearch'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'airflow')\gexec
