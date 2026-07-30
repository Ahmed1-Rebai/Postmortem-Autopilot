# The backup CronJob's scale-down/scale-up steps need a `kubectl` binary
# and a shell to copy it onto a shared emptyDir — measured, not assumed:
# `bitnami/kubectl` no longer publishes version-pinned tags matching this
# project's k3s version, and `rancher/kubectl` is a genuinely distroless
# image (confirmed: no `sh`, no coreutils, just the static binary at
# /bin/kubectl), which can't run the copy step at all. `alpine/k8s` works
# but bundles kubectl+helm+kustomize+more at ~850 MB for a one-binary need.
# This is ~15 MB: alpine base + one static binary, nothing else.
FROM alpine:3.20

ARG KUBECTL_VERSION=v1.30.6
ADD https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl /usr/local/bin/kubectl
RUN chmod +x /usr/local/bin/kubectl
