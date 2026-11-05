# Credential and execution boundaries

Never commit ClickUp tokens, GitHub tokens, .env files, runtime receipts, real task bindings, private descriptions or logs. Use placeholders in issues and redact headers/credentials before sharing diagnostics.

GitHub mode deliberately uploads config, specs and bindings plus the bundled worker to a scoped branch in the configured fork. It does not upload the whole checkout or .env. Branch contents and result artifacts can contain task data: choose the fork's visibility and access accordingly. Secret-backed workflows execute the installed tool's bundled worker, so review and trust the tool version you install.

Read/preview GitHub commands still create a branch and Actions run. --apply permits selected ClickUp mutations. A failed request may have partially succeeded; inspect state before retrying. Installer refuses unrelated existing commands/directories and does not modify provider or shell startup configuration.

Do not post a live token in a GitHub issue. Repository access and distribution/license decisions remain with the repository owner.
