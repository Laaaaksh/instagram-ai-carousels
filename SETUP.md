# Instagram auto-publisher setup

GitHub Actions checks schedule.json every 15 minutes and publishes each post at its time. You do steps 1 to 5 once. Meta changes button labels often, so expect small differences.

## 1. Switch to a professional account

Instagram app > Settings > Account type and tools > Switch to professional account. Pick Creator. The API refuses personal accounts.

## 2. Create a Meta app

1. Go to developers.facebook.com and log in.
2. My Apps > Create app.
3. Use case: "Manage messaging and content on Instagram". App type: Business.
4. Open the app. Go to Instagram > API setup with Instagram business login.
5. Under "Generate access tokens", add @lakshsadhwani__ and log in to Instagram when asked.
6. Approve both permissions: instagram_business_basic and instagram_business_content_publish.
7. Copy the token. The token lasts 60 days. The weekly refresh job renews the token for you.

Keep the app in development mode. Development mode works for your own account and needs no review from Meta.

## 3. Save the token in GitHub

In Terminal, inside this folder, run:

    gh secret set IG_ACCESS_TOKEN

Paste the token when asked and press Enter. GitHub stores the token encrypted. The token never goes in a file or in chat.

## 4. Test before the first post

    read -s IG_ACCESS_TOKEN && export IG_ACCESS_TOKEN && python3 ig_publish.py check

Paste the token and press Enter. The token stays hidden and out of your Terminal history. You should see your username, your remaining quota, and "all 56 unpublished images reachable as JPEG".

## 5. Turn on token refresh

1. GitHub > Settings > Developer settings > Fine-grained tokens > Generate new token.
2. Repository access: this repo only. Permission: Secrets, read and write.
3. Copy the token, then run `gh secret set SECRETS_PAT` and paste.

Without this step, publishing stops when the Instagram token expires after 60 days.

## Everyday commands

    python3 ig_publish.py status                      # see the schedule
    python3 ig_publish.py shift --start 2026-10-09    # move all unpublished posts, same order
    python3 build.py && python3 ig_publish.py prepare # after editing slides or captions

Push to GitHub after any change: `git add -A && git commit -m "update" && git push`

## When something fails

GitHub emails you when a run fails. Run `python3 ig_publish.py status` to read the error.

- missed: the post ran more than 6 hours late. Fix with `shift`.
- failed: three tries failed. Fix the cause in the error, then `shift`.
