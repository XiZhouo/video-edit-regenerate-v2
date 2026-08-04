# Artifact Variant contracts

This directory owns the machine contracts that separate reusable runtime bytes from scoped product presentation.

- `ze.artifact-build.v1`: is the environment-neutral producer declaration in `ZE_ARTIFACT_BUILD.json`. It selects runtime variants, delivery modes, artifact roles, feature sets, payload roots, and optional Cloudflare runtime-package shape; it cannot contain a Series, target, host, port, account, resource id, credential, or private key.
- `ze.artifact-variant-manifest.v1`: closes over source identity and a delivery matrix of code-affecting variant inputs, actual Base Artifacts, CycloneDX SBOMs, Ed25519 detached signatures, optional Series overlays, update-candidate metadata, and isolation policy. Creator-hosted API and hybrid modes require both halves of the delivery.
- `ze.series-overlay.v1`: contains exactly one Skill item projected from one System `ze.catalog-snapshot.v1`, plus the Base digest and Catalog snapshot identity used to produce it.
- `ze.source-inventory.v1`: records deterministic source-selection policy and every selected file digest/size. The validator re-enumerates the local source before accepting `source_digest`.
- `ze.runtime-package.v1`: is the signed archive's provider-runnable manifest at `ze-runtime.json`. Cloudflare variants use it to declare the exact payload root, Worker entrypoint or static directory, binding names, runtime environment names, health path, and honest source-visibility boundary. Deploy must consume this manifest instead of guessing from filenames or rebuilding source.

The Base Artifact is global and cannot contain a concrete Series, Organization, Target, Series display name, theme reference, or Series icon binding. An overlay is intentionally scoped and must retain `authorization.evaluation = runtime-check-required`; it is presentation data, not an entitlement or deployment binding. Every Base and Overlay has its own detached Ed25519 signature. Only the public key and detached signature are referenced by the manifest; release private keys must remain outside the Skill, source inventory, artifacts, and Git.

The actual producer is `scripts/release_builder.py`. `--check` validates the declaration and delivery matrix without side effects. A release build obtains build-only dependencies through the Environment Broker, executes the one declared build argv with `shell=False`, and gives that subprocess only a minimal runtime environment instead of the caller's Cloudflare/SSH/session credentials. It packages deterministic ZIP bytes, emits CycloneDX SBOMs and Ed25519 signatures, writes the Variant Manifest last, and runs `artifact_variant.py` over the final closure. Its managed Python requirements are declared in `scripts/TOOLCHAIN_PY_DEPS.json`; Skill.Sync reports missing target dependencies without rewriting target-owned requirements.

```bash
python3 scripts/release_builder.py . --check
python3 scripts/release_builder.py . \
  --private-key /secure/release-ed25519.pem \
  --key-id release-main
```

`ZE_ARTIFACT_VARIANT.json` is required for a portable recipe that claims scoped-release readiness. Ready Build Evidence projects each selected `runtime_variant` and validated runtime-package digest beside its signed artifact identity, so Deploy can match and materialize the artifact without guessing from the archive. Existing legacy/v2 recipes remain readable and are explicitly reported as `legacy-unscoped`.
