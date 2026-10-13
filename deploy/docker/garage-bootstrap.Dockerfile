# syntax=docker/dockerfile:1.7

# The official Garage image is intentionally shell-less. This helper keeps
# the reviewed Garage CLI binary while adding only the pinned BusyBox shell
# needed for idempotent bootstrap orchestration.
ARG GARAGE_IMAGE=dxflrs/garage:v2.1.0@sha256:4c9b34c113e61358466e83fd6e7d66e6d18657ede14b776eb78a93ee8da7cf6a
ARG BUSYBOX_IMAGE=busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e

FROM ${GARAGE_IMAGE} AS garage
FROM ${BUSYBOX_IMAGE}

COPY --from=garage /garage /garage

ENTRYPOINT ["/bin/sh"]
