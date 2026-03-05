# Airbyte API: Connection Create 500 — Causes and Fixes

When creating a connection via `POST /v1/connections`, Airbyte Cloud can return **500** with:

```json
{"data": {"message": "Something went wrong in the connector. See the logs for more details."}}
```

Programmatic connection create **is supported** by Airbyte; the failure is usually due to validation or environment, not “API not supported.”

---

## 1. **Stream discovery fails or returns empty (most likely)**

When you create a connection, the backend typically:

- Runs **source discovery** (or uses a cached catalog) to know which streams exist.
- Validates that the requested streams exist and are compatible with the destination.

If **GET /streams** returns no streams (e.g. script falls back to `"documents"`), then:

- The backend may run discovery again and get **no streams** from the Google Drive source.
- “No streams” is often reported as a generic connector/500 error.

**Causes for “no streams” from Google Drive:**

- **Empty folder** — No files in the synced folder.
- **Permissions** — OAuth token missing `https://www.googleapis.com/auth/drive.readonly` or folder not shared with the authenticated account.
- **Source just created** — Discovery run too soon after creating the source; backend may need a short delay.
- **Stream config** — Wrong or unsupported `format` / `globs` in the source’s `streams` config (e.g. no files match), or connector expects a specific stream setup (see [discussion #33850](https://github.com/airbytehq/airbyte/discussions/33850)).

**What to do:**

- Ensure the Drive folder has at least one file and the same account can open it in the browser.
- Only create the connection **after** **GET /streams** returns at least one stream; do not fall back to `"documents"` if discovery actually returned empty.
- Increase timeout and retries for **GET /streams** (e.g. 120–180s, 2–3 retries with backoff).
- In source config, set explicit `format` and `globs` (e.g. `**` or `*.pdf`) so the connector can discover streams.

---

## 2. **Destination (Pinecone) check fails**

Creating a connection can trigger a **destination check** (Pinecone index exists, dimensions match, API key valid).

**Typical causes:**

- **Index missing** — The index name in the destination config does not exist in Pinecone.
- **Dimension mismatch** — Index dimension (e.g. 1536 or 1024) does not match the embedding dimensions in the destination config.
- **Wrong region / environment** — `pinecone_environment` (e.g. `us-east-1`) does not match the index.
- **Invalid or expired API key** — Pinecone returns 4xx and the connector reports a generic failure.

**What to do:**

- Create the Pinecone index in the same region with the correct dimension **before** creating the destination/connection.
- Use the same `index` name, `pinecone_environment`, and `dimensions` in the destination config as in your Pinecone console.

---

## 3. **Source check fails**

The backend may run a **source check** (list files, validate credentials).

**Typical causes:**

- **Invalid or expired OAuth** — Refresh token revoked or credentials wrong.
- **Scope** — Missing `drive.readonly` (or required Drive scope).
- **Auth type** — Some connectors require `auth_type: "Client"` in credentials; missing it can cause 500 (see [issue #70909](https://github.com/airbytehq/airbyte/issues/70909)).

**What to do:**

- Use valid OAuth credentials with `client_id`, `client_secret`, `refresh_token` and, if required, `auth_type: "Client"`.
- Ensure the token was obtained with the correct Drive scopes and that the folder is accessible to that account.

---

## 4. **Request payload or API usage**

- **Stream list** — Backend may expect stream names that came from a prior successful discovery; using a hardcoded stream name that doesn’t exist can cause 500.
- **Timeout** — Discovery or check can be slow; 500 can be a timeout. Use long timeouts (e.g. 120s) for **GET /streams** and **POST /connections** (see [issue #29506](https://github.com/airbytehq/airbyte/issues/29506), [terraform-provider #129](https://github.com/airbytehq/terraform-provider-airbyte/issues/129)).
- **Schedule** — Some schedule formats (e.g. cron) might not be accepted on create; creating with `scheduleType: "manual"` then **PATCH**ing the schedule can avoid that.

---

## 5. **Data activation / vector destinations**

Pinecone is a “vector”/data-activation-style destination. The API may still require:

- A valid **catalog** (streams that actually exist on the source).
- Correct **stream → destination** mapping (e.g. which field is used for text for embedding).

So the same rules apply: discovery must succeed and stream names must match what the source exposes.

---

## 6. **What is supported**

- **Creating sources, destinations, and connections via API** — Supported on Airbyte Cloud.
- **Scheduling** — Manual, scheduled (e.g. every 2 hours), or cron; can be set on create or via PATCH.
- **Multiple connections per source** — One source can feed multiple destinations (e.g. two Pinecone indexes).

So “we want to do it via code” is supported; the 500 is almost always a configuration or environment issue, not “feature not supported.”

---

## Recommended flow for your app (clients → Drive → Pinecone)

1. **Create source** (Google Drive, folder URL, OAuth from your client).
2. **Create destination(s)** (Pinecone, one per index; correct dimensions and index name).
3. **Wait 10–30 s** after creating the source (optional but can help).
4. **GET /streams** with `sourceId` (and optionally `destinationId`), long timeout (e.g. 120s), retries.
5. **If and only if** the response contains at least one stream, build `configurations.streams` from that response (name + syncMode).
6. **POST /connections** with that stream list, `namespaceDefinition`/`namespaceFormat` as needed, and e.g. `scheduleType: "manual"` (then PATCH schedule if you want every 2 hours).
7. If you still get 500:
   - Confirm the Drive folder has files and the same user can open them.
   - Confirm Pinecone index exists, dimension matches, and API key is valid.
   - Check Airbyte Cloud connection/sync logs for the real error (connector logs often say “no streams” or “check failed”).
   - Consider opening a support ticket with Airbyte with workspace ID, source/destination IDs, and the request body (redact secrets).

---

## Why source and destination can be correct but connection still fails

You can verify **outside** Airbyte that:

- **Source:** Token works, folder is listable, doc/sheet content is readable (e.g. `scripts/check_drive_source_content.py`).
- **Destination:** Pinecone indexes exist, dimensions match, API key works (e.g. `scripts/test_2_pinecone_destination.py`, `scripts/check_pinecone_indexes.py`).

Airbyte can still return **500** on **GET /streams** or **POST /connections** because:

1. **Discovery runs inside Airbyte’s workers**  
   Stream discovery is executed by Airbyte Cloud in their environment, not with your local script. So:
   - Credentials are sent to their API and used by their connector process.
   - Their Google Drive connector may use a slightly different config shape, API, or version than the one you use locally (e.g. `delivery_method` missing, or a different stream format).
   - Their Pinecone connector may run an extra “check” (e.g. test write) that you don’t run locally; that check can fail and surface as a generic 500.

2. **GET /streams can trigger both source and destination**  
   For `GET /streams?sourceId=X&destinationId=Y`, the backend may run **source** discovery and/or **destination** compatibility. If **either** the Google Drive connector or the Pinecone connector fails in that flow (e.g. timeout, exception, or returning a null/empty catalog), the API often returns the same generic 500. So the failing “connector” in the message might be the destination, not the source, even when the source is correct.

3. **Source config shape vs Cloud**  
   The connector on Cloud may expect fields we don’t send (e.g. `delivery_method` with `delivery_type: "use_records_transfer"`) or a different stream format. If the connector receives an unexpected config, it can throw during discover and the platform returns 500.

4. **Backend catalog is null**  
   Some failures are reported as “discovered is null” (see [issue #36745](https://github.com/airbytehq/airbyte/issues/36745)): the connector’s discover step doesn’t return a valid catalog. The platform then throws when building the response. So “Something went wrong in the connector” can mean “discover failed or returned nothing,” not only “credentials wrong.”

**What to do:**

- Add **delivery_method** to the source configuration when creating the source (see script below).
- Try **GET /streams** with **only** `sourceId` (no `destinationId`) to see if discovery works without the destination in the path (some deployments use destination only when creating the connection).
- Check **Airbyte Cloud logs** for the source and destination (connection/sync/job logs). The real error (e.g. “No streams,” “check failed,” or an exception message) is usually there.
- If possible, **create the same source and connection once in the Airbyte UI** (same folder, same OAuth, same Pinecone index). If it works in the UI, the difference is in the API payload (e.g. stream config, delivery_method, or how the connection is created).
- As a last step, **open a support ticket** with Airbyte (workspace ID, source ID, destination ID, and redacted request bodies) so they can confirm which connector is failing and why.

---

## Diagnostic scripts

Run these from the project root to isolate the cause:

| Script | Tests (doc section) |
|--------|----------------------|
| `python scripts/test_1_stream_discovery.py` | §1 — GET /streams returns streams? (set `SOURCE_ID`, optional `DEST_ID`) |
| `python scripts/test_2_pinecone_destination.py` | §2 — Pinecone index (pm-test) exists, dimension 1536 |
| `python scripts/test_3_google_drive_source.py` | §3 — OAuth valid, folder listable (tokens.json + Drive folder) |
| `python scripts/test_4_connection_create.py` | §4 — POST /connections (set `SOURCE_ID`, `DEST_ID`); use `DRY_RUN=1` to only print payload |

Run all: `bash scripts/run_connection_500_tests.sh` (set `SOURCE_ID` and `DEST_ID` in env for tests 1 and 4).

---

## References

- [Airbyte API documentation](https://docs.airbyte.com/developers/api-documentation)
- [Configuring API access](https://docs.airbyte.com/platform/using-airbyte/configuring-api-access)
- [Sync schedules](https://docs.airbyte.com/platform/using-airbyte/core-concepts/sync-schedules)
- [Pinecone destination](https://docs.airbyte.com/integrations/destinations/pinecone)
- [Google Drive “No streams” discussion](https://github.com/airbytehq/airbyte/discussions/33850)
- [Connection create timeout / 500](https://github.com/airbytehq/airbyte/issues/29506)  
- [GA4 OAuth auth_type and 500](https://github.com/airbytehq/airbyte/issues/70909)
