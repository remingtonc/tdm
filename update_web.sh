#!/usr/bin/env bash
COMPOSE_FILE=""
case "$1" in
    ""|"http")
        COMPOSE_FILE=compose.yaml
        ;;
    "https")
        COMPOSE_FILE=compose.https.yaml
        ;;
    *)
        echo $"Usage: $0 [http|https]"
        exit 1
esac
podman compose -f $COMPOSE_FILE up -d --force-recreate --build --no-deps web
