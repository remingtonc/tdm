#!/usr/bin/env bash
podman build -t tdm/doc_test .
podman run -p 8089:8080 -v $(pwd)/docs:/data/docs:rw,Z --name tdm_dev_docs tdm/doc_test docs:dev
