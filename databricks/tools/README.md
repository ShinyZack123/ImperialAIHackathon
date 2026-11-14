# Databricks CLI setup

Install the current official Databricks CLI for your operating system using the [Databricks installation instructions](https://docs.databricks.com/aws/en/dev-tools/cli/install). The executable is deliberately not included in this repository. Do not install the legacy `databricks-cli` Python package as a substitute.

The initial integration was validated using CLI v1.26.0 on macOS Apple Silicon. Its official archive was checked against the published SHA-256 checksums before installation. Other supported operating systems should use the matching official build.

With the CLI on PATH, sign in yourself:

```sh
databricks auth login --host https://your-workspace.cloud.databricks.com --profile gpu-ops
```

Replace the host with the same workspace URL configured in `../.env`. Complete the browser sign-in. Credentials remain in the CLI's local configuration/cache and must not be committed or copied into frontend code.

`DATABRICKS_CLI_PATH=databricks` uses PATH. If you installed the executable in a project-local `tools/` directory, set its absolute path in your ignored `.env`. A project-local `tools/databricks` binary is ignored by Git.

References: [official CLI release](https://github.com/databricks/cli/releases/tag/v1.26.0), [OAuth user sign-in](https://docs.databricks.com/aws/en/dev-tools/auth/oauth-u2m), [CLI license](https://github.com/databricks/cli/blob/v1.26.0/LICENSE).
