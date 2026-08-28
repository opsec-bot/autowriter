# Google credentials, click by click

`check`, `plan` and `inspect` need none of this. Only `copy` — the command that
writes a real Google Doc — needs an account.

**Run `autowriter setup` first.** It names the one thing to do next and prints
the command that does it, so most of this file is only worth reading for the
step you are actually stuck on. Run it again after each step; it is the thing
that decides whether you are finished, and it exits 0 when you are.

If `gcloud` is installed, the whole of this file collapses to two commands and
no console clicking at all:

```bash
gcloud services enable docs.googleapis.com drive.googleapis.com
gcloud auth application-default login   --scopes=https://www.googleapis.com/auth/documents,https://www.googleapis.com/auth/drive.file
```

## Contents

- Step 1: a Google Cloud project
- Step 2: enable the two APIs
- Step 3: pick a credential type
- Option A: OAuth client (a person copying their own documents)
- Option B: service account (a script, a server, a cron job)
- Option C: gcloud application default credentials
- Where the token is stored
- Troubleshooting

## Step 1: a Google Cloud project

1. Open <https://console.cloud.google.com/projectcreate>.
2. Name it anything (`autowriter` is fine) and click **Create**.
3. Make sure it is the selected project in the bar at the top.

A project is free. Both APIs used here are free at these volumes.

## Step 2: enable the two APIs

Both are required. Click each link with the project selected and press
**Enable**:

- Google Docs API — <https://console.cloud.google.com/apis/library/docs.googleapis.com>
- Google Drive API — <https://console.cloud.google.com/apis/library/drive.googleapis.com>

Enabling takes a few seconds and only has to be done once per project.

## Step 3: pick a credential type

| You are | Use | Why |
|---|---|---|
| A person copying your own documents | **Option A**, OAuth client | The doc lands in your own Drive, owned by you |
| A script, server or scheduled job | **Option B**, service account | No browser, no human |
| Already using `gcloud` | **Option C**, ADC | Nothing more to download |

## Option A: OAuth client (recommended for people)

1. Go to <https://console.cloud.google.com/apis/credentials/consent>.
   Choose **External**, fill in an app name and your own email for both support
   and developer contact, and save. You do not need to publish or verify the
   app.
2. On the **Audience** (or **Test users**) screen, add your own Google address
   as a test user. Without this the sign-in fails with `access_denied`.
3. Go to <https://console.cloud.google.com/apis/credentials> →
   **Create credentials** → **OAuth client ID** → **Desktop app** → **Create**.
4. Click **Download JSON**. Save it as `client_secret.json` somewhere outside
   the repository.
5. Run:

   ```bash
   autowriter copy report.docx --client-secrets client_secret.json
   ```

   A browser opens once. Approve the two scopes. The token is cached, so later
   runs need no browser — and if you set `AUTOWRITER_CLIENT_SECRETS` to that
   path, no flag either.

## Option B: service account (recommended for automation)

1. Go to <https://console.cloud.google.com/iam-admin/serviceaccounts> →
   **Create service account**. Name it, click through, **Done**. No roles are
   needed: the Docs and Drive scopes are granted by the key itself.
2. Open the new account → **Keys** → **Add key** → **Create new key** →
   **JSON**. A file downloads. Treat it like a password.
3. Run:

   ```bash
   autowriter copy report.docx --service-account key.json
   ```

The document is created in the *service account's* Drive, not yours. Two ways
to reach it:

- Share a folder in your Drive with the service account's email address
  (`name@project.iam.gserviceaccount.com`), create an empty doc inside it, and
  pass `--document-id` — the copy is written into that document, which you own.
- Or, on a Google Workspace domain, configure domain-wide delegation and
  impersonate a user.

## Option C: application default credentials

```bash
gcloud auth application-default login \
  --scopes=https://www.googleapis.com/auth/documents,https://www.googleapis.com/auth/drive.file
autowriter copy report.docx
```

Nothing else to pass — ADC is the last source tried, so it is used when no key
and no client secrets are given.

## Where the token is stored

`~/.autowriter/token.json`, created with `0600` permissions after the first
OAuth sign-in. Delete it to force a fresh sign-in. Override the location with
`--token-file`.

Environment variables, if you prefer them to flags:

```bash
export AUTOWRITER_SERVICE_ACCOUNT=/path/to/key.json
export AUTOWRITER_CLIENT_SECRETS=/path/to/client_secret.json
```

## Troubleshooting

`autowriter setup` recognises most of these and prints the fix. The table is
for the ones it hands you verbatim from Google.

| Message | Cause | Fix |
|---|---|---|
| `The Google API client libraries are required` | Installed without the extra | `pip install "autowriter[google]"` |
| `Not signed in yet` | Client secrets configured, no cached token | `autowriter setup --login --client-secrets client_secret.json` |
| `403 Google Docs API has not been used in project ... before or it is disabled` | Step 2 skipped or wrong project | Enable both APIs in the project the credentials belong to |
| `access_denied` in the browser | Your address is not a test user on the consent screen | Add it under **Audience** / **Test users** |
| `invalid_grant` | Cached token no longer valid | `rm ~/.autowriter/token.json` and run again |
| `insufficient authentication scopes` | ADC minted without the two scopes | Re-run the `gcloud` command in Option C with `--scopes` |
| `429 Too Many Requests`, run slows down | Per-minute write quota | Expected; the client backs off 20s, 40s, 60s and continues |
| Copy succeeds but you cannot find the document | Service account owns it | Use `--document-id` with a doc in a folder you shared, as above |
