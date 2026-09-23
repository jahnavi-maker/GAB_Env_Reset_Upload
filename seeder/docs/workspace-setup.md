# How you push data into all 200 accounts

You do this once. After it works, the site’s **Push all** button fills every `userN@deccanexperts.us` mailbox.

You need two browser logins:

1. [admin.google.com](https://admin.google.com) — the **company** admin for `deccanexperts.us`
2. [console.cloud.google.com](https://console.cloud.google.com) — the same Google Cloud project you already used for OAuth

If step 0 fails, stop. You cannot finish this until someone who is a Workspace Super Admin does it, or makes you one.

---

## Step 0 — check you are the admin

1. Open [admin.google.com](https://admin.google.com)
2. Sign in with your work account (the one that manages `deccanexperts.us`)
3. You should see a Home page with **Users**, **Apps**, **Security**
4. Open **Directory → Users** and confirm you can see `user1@deccanexperts.us` (and the rest)

If Google says you do not have permission, you are not the admin. Ask whoever created those 200 accounts to either do these steps, or make you a Super Admin, then start again at step 0.

Each of those 200 users also needs a **paid Workspace licence** that includes Gmail, Calendar, and Drive (not Cloud Identity Free).

---

## Step 1 — open the Cloud project

1. Open [console.cloud.google.com](https://console.cloud.google.com)
2. Top bar: click the project name → pick the **same project** you used when you downloaded the OAuth JSON for this tool
3. Left menu: **APIs & Services → Library**
4. Search and **Enable** each of these if they are not already on:
   - Gmail API
   - Google Calendar API
   - Google Drive API

---

## Step 2 — create a robot account (service account)

This is not a person. It is a key the tool uses so 200 people do not have to click Authorize.

1. Left menu: **IAM & Admin → Service accounts**
2. Click **+ Create service account**
3. Name: `gab-seed`
4. Click **Create and continue**
5. Skip the optional permission screens. Click **Done**
6. Click the new row `gab-seed`
7. Open the **Keys** tab
8. **Add key → Create new key → JSON → Create**
9. A file downloads. Move it to:

   `/Users/divya/Downloads/gab-sa.json`

   Do not put this file in the project folder, in git, or in the zip.

---

## Step 3 — copy the number, not the email

Still on the `gab-seed` service account page:

1. Open the **Details** tab
2. Find **Unique ID** / **Client ID** — a long number like `1023…`
3. Copy that number
4. Ignore the `gab-seed@….iam.gserviceaccount.com` email. You do not paste that.

---

## Step 4 — allow that robot to act as any company user

1. Open [admin.google.com](https://admin.google.com)
2. **Security → Access and data control → API controls**
3. Scroll to **Domain-wide delegation**
4. Click **Manage Domain Wide Delegation**
5. Click **Add new**
6. **Client ID** = the long number from step 3
7. **OAuth scopes** = paste this whole line, exactly, with the commas:

```
openid,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/gmail.modify,https://www.googleapis.com/auth/calendar,https://www.googleapis.com/auth/drive
```

8. Click **Authorize**

That is the one-time Google setup. You never do steps 1–4 again unless the key is lost.

---

## Step 5 — start this tool in company mode

In Terminal:

```bash
export ENV_LOADER_AUTH_BACKEND=workspace_delegation
export ENV_LOADER_SA_KEY=/Users/divya/Downloads/gab-sa.json
export ENV_LOADER_WORKSPACE_DOMAIN=deccanexperts.us
export GAB_PUSH_THREADS=10
export GAB_PUSH_USERS_PER_THREAD=20
cd /Users/divya/Downloads/DesignBench/GAB/gab-workspace-seed
./start.sh
```

Open http://127.0.0.1:8765 and hard-refresh (Cmd+Shift+R).

You should see `workspace_delegation`. There is **no Authorize** button. That is correct.

---

## Step 6 — load the sheet and push

1. **Choose CSV** → the 200-row Account mapping file
2. Confirm the table shows `user1@deccanexperts.us` … `user200@deccanexperts.us`
3. In **05 · Push**, leave Calendar, Gmail, Drive on
4. Click **Push all matched accounts**
5. Leave the tab open. Default batch is 10 threads × 20 users (200 at once). Change Threads / Users per thread in 05 · Push. Failures show in the red log, `runs/…/failures.log`, and one file per account in `runs/…/logs/`.

---

## If something blocks you

| What you see | What it means |
| --- | --- |
| admin.google.com says no permission | You are not the Workspace admin. Stop and get Super Admin. |
| No Domain-wide delegation page | Same — you are not admin. |
| Push all missing / site still wants Authorize | You started the tool without the `export` lines. Do step 5 again. |
| Failures about not in domain | The CSV email is not `@deccanexperts.us`. |
| Failures about insufficient permission / not authorized | Step 4 scopes are wrong, or the Client ID does not match the JSON key. |
