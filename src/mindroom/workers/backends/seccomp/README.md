# Worker computer seccomp profile

`worker-computer.json` is an OCI-compatible profile derived from Moby's maintained default seccomp policy at tag
`docker-v29.8.0`:

- Source: `https://github.com/moby/moby/blob/docker-v29.8.0/vendor/github.com/moby/profiles/seccomp/default.json`
- Source SHA-256: `536529b665dd0972c37bfb569f5d4ac8a53592e7b00752bc39ff063ca9864c74`
- Packaged SHA-256: `578ef2b662d8e9a886132148a0b75f0388efff92ed902b16d7be2777ae3788fa`

The profile resolves Moby's runtime extensions for a capability-free worker and removes the extension-only
`archMap`, `includes`, and `excludes` keys so Docker and Kubernetes CRI runtimes enforce the same OCI policy. It adds
only the Chromium sandbox operations observed on Linux: argument-filtered `clone` for `CLONE_NEWUSER`,
`CLONE_NEWPID`, and `CLONE_NEWUSER|CLONE_NEWPID|CLONE_NEWNET`; argument-filtered `unshare(CLONE_NEWUSER)`; and
`chroot`, which still requires namespace-local kernel authority. `clone3` keeps the default `ENOSYS` behavior.

Rebase and revalidate this snapshot when the container runtime or packaged Chromium version changes.

The namespace filters assume Linux syscall argument layouts where `clone` flags are argument 0, including aarch64
and x86_64. The kernel must permit unprivileged user namespaces; seccomp cannot override a host sysctl or LSM denial.
Live Chromium validation was performed on Linux aarch64 with Docker and containerd. Other architectures, including
x86_64, require validation on the target kernel/runtime before deployment; Moby's upstream architecture coverage
does not imply that this derived Chromium profile was tested across that matrix.
