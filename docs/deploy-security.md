# Deploy security — two settings to apply on EC2

These are **deployment settings only**. No application code changes; the flow is
unchanged. They close the two remaining items from the security audit.

---

## 1. Lock the operator pages to your team (network boundary)

The onboarding pages run in a browser and call same-origin helper endpoints that
cannot carry the Bearer secret (a browser can't hold a secret safely). So instead of
app-level auth, restrict them at nginx to your team's IPs / VPN. Freelancers never use
these — they only use `/reset` and the reset APIs, which stay public.

**Operator-only paths to restrict:**
- `/onboard`, `/onboard/authorize`, `/authorized`
- `/ui/authorize`, `/ui/seed`, `/ui/recover`, `/ui/client`, `/ui/upload`, `/ui/account`

**Public paths (leave open):**
- `/reset`, `/reset/status/...`, `/ui/task`, `/ui/task/reset`, `/ui/reset/...`,
  `/ui/freelancer/verify`, `/ui/auth-config`
- `/api/environment/reset` (Bearer), `/api/freelancers` (Bearer), `/healthz`

**nginx example** (replace the IPs with your office/VPN CIDRs):

```nginx
# Operator-only: onboarding + its helper endpoints
location ~ ^/(onboard|authorized|ui/(authorize|seed|recover|client|upload|account)) {
    allow 203.0.113.0/24;    # your office / VPN CIDR
    allow 198.51.100.10;     # a specific admin IP
    deny  all;
    proxy_pass http://127.0.0.1:8791;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}

# Everything else (freelancer reset page + Bearer APIs + health) stays public
location / {
    proxy_pass http://127.0.0.1:8791;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

Result: only your team can open the onboarding UI or hit its endpoints; the destructive
`/ui/reset` and PII `/ui/accounts` `/ui/sessions` endpoints were already removed from the app.

---

## 2. Enforce signed freelancer reset links

Set `RESET_LINK_SECRET` on EC2 (any long random string). With it set, `/ui/task/reset`
**requires a valid signed token** — a freelancer can only reset the one account their
link was minted for, and cannot edit the URL to target another account. Without it (dev
only) the platform falls back to the raw path and logs a warning.

```bash
# in the EC2 .env / systemd EnvironmentFile
RESET_LINK_SECRET=<long-random-string>
```

Mint a freelancer link with `POST /api/reset-link` (Bearer) → returns the `/reset#t=<token>` URL.

---

## Already handled in code (no action needed)
- Removed the open destructive endpoint `POST /ui/reset` and the PII endpoints
  `/ui/accounts`, `/ui/sessions` (and the dead dashboard/simple pages).
- Bearer required on every `/api/*`; constant-time comparison.
- Google sign-in verifies the ID token server-side (audience + issuer + email_verified).
- Error responses no longer leak internal detail; secrets are never logged.
