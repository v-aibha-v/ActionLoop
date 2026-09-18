# Google OAuth 2.0 setup

ActionLoop needs Google access for exactly two things: creating a Calendar event for an approved action item, and creating a Gmail **draft**. This guide covers the console clicks, the local configuration, and the errors you are most likely to hit.

Google is only required at the execution step. You can build, run, test and review the whole extraction pipeline with just an Anthropic key.

## 1. Create a project

1. Open the [Google Cloud Console](https://console.cloud.google.com/).
2. Project selector (top left) -> **New Project**. Name it something like `ActionLoop`.
3. Make sure the new project is the selected one before continuing.

## 2. Enable the two APIs

**APIs & Services -> Library**, then enable each of:

- **Google Calendar API**
- **Gmail API**

Enabling the Gmail API is required for drafts. It does not grant mailbox access — the scope you consent to does not include reading mail.

## 3. Configure the OAuth consent screen

**APIs & Services -> OAuth consent screen**

1. User type: **External** (Internal is only available on Workspace accounts).
2. App name: `ActionLoop`. Add your own address as the support email.
3. **Scopes**: you can leave this empty. ActionLoop requests its scopes at runtime, and they appear on the consent screen then.
4. **Test users**: add the Google account you will connect with. While the app is in *Testing* mode, only listed test users can authorise it.
5. Leave the publishing status as **Testing** for local development. Refresh tokens issued in testing mode can expire after 7 days; when that happens, reconnect and the cached token is replaced.

## 4. Create the OAuth client

**APIs & Services -> Credentials -> Create credentials -> OAuth client ID**

1. Application type: **Web application**.
2. Name: `ActionLoop local`.
3. **Authorised redirect URIs -> Add URI**:

   ```text
   http://localhost:8000/auth/google/callback
   ```

   This must match `GOOGLE_REDIRECT_URI` character for character. A trailing slash, a different port, or `127.0.0.1` instead of `localhost` all produce `redirect_uri_mismatch`.

4. **Create**, then copy the client ID and the client secret.

## 5. Configure the application

Add the values to your git-ignored `.env`:

```bash
GOOGLE_CLIENT_ID=<your-client-id>.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=<your-client-secret>
GOOGLE_REDIRECT_URI=http://localhost:8000/auth/google/callback
GOOGLE_TOKEN_PATH=.secrets/google_token.json
GOOGLE_CALENDAR_ID=primary
GOOGLE_CALENDAR_TIMEZONE=UTC
```

Never commit `.env`. It is already git-ignored, as are `.secrets/`, `credentials.json`, `client_secret*.json` and `*token*.json`. The values above are placeholders — no real credential appears anywhere in this repository.

## 6. Complete the consent flow

1. Start the app:

   ```bash
   uvicorn app.main:create_app --factory --reload
   ```

2. Either open <http://localhost:8000> and press **Connect Google**, or call the endpoint directly:

   ```bash
   curl http://localhost:8000/auth/google
   # {"authorization_url": "https://accounts.google.com/o/oauth2/v2/auth?...",
   #  "state": "…", "scopes": [".../auth/calendar.events", ".../auth/gmail.compose"]}
   ```

3. Open the returned `authorization_url`, choose your account, and read the consent screen. You should see **Calendar events** and **Create, edit and send email drafts** — and nothing about sending mail.
4. Google redirects back to `/auth/google/callback`, which validates the `state` value, exchanges the code, writes the token to `GOOGLE_TOKEN_PATH`, and forwards the browser to the UI with a `google=connected` flag.
5. Verify:

   ```bash
   curl http://localhost:8000/auth/google/status
   # {"configured": true, "authenticated": true,
   #  "scopes": [".../auth/calendar.events", ".../auth/gmail.compose"],
   #  "connected_email": "you@example.com"}
   ```

`connected_email` is best-effort. It is read from the Gmail profile, so if Google cannot be reached the field is `null` while the rest of the status still works.

## 7. Verify the side effects

Approve an action item that has a deadline, then execute it. You should get back an event link and a draft id, and:

- the Calendar event exists on `GOOGLE_CALENDAR_ID` (default: your primary calendar);
- a Gmail **draft** exists, addressed to `GMAIL_FOLLOW_UP_RECIPIENT` if set, otherwise with no recipient at all.

It is not sent. Nothing in the application can send it.

## Managing the session

| Goal | How |
| --- | --- |
| See whether you are connected | `GET /auth/google/status` |
| Reconnect or switch accounts | `GET /auth/google` again, or the **Reconnect Google** button |
| Forget the cached token | `DELETE /auth/google`, or delete the file at `GOOGLE_TOKEN_PATH` |

The token file holds a refresh token. Treat it exactly like a password: it is git-ignored, and it belongs to whichever account completed the consent flow.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `redirect_uri_mismatch` | `GOOGLE_REDIRECT_URI` does not match an authorised URI in the console exactly. Add the precise string you configured. |
| `access_denied` | The signed-in account is not in the consent screen's test-user list, or you declined. |
| `Google OAuth is not configured` (`502`) | `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` are empty in `.env`. |
| `401 google_not_authenticated` on execute | No cached token yet, or it was deleted. Connect Google first — nothing was marked as failed. |
| `invalid_grant`, or you must reconnect constantly | The refresh token expired (common in testing mode) or was revoked. Reconnect; if it recurs, publish the consent screen or accept the 7-day cycle. |
| `403 insufficientPermissions` | The Calendar or Gmail API is not enabled in the project, or consent was granted before a scope was added. `DELETE /auth/google`, then reconnect. |
| A draft but no Calendar event | That item had no deadline, so Calendar was deliberately skipped. The outcome detail says exactly that. |
| `429` / `503` from Google | Rate limiting or a transient outage. The item is marked `failed` with the message; retry execution — existing resources are reused, not duplicated. |