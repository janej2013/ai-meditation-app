# What Drift keeps, and for how long

A plain-language account of the personal information this product handles, written from the
code rather than from intentions. Every statement below names the place that enforces it, so a
reader who wants to check can. It is not legal advice and not yet a reviewed privacy policy.
The in-app version is `/privacy` (`frontend/src/pages/PrivacyPage.tsx`); this file is the
annotated source, and a test holds the page's numbers to it. Last checked against the code:
2026-08-26.

Everything runs in AWS's Sydney region (`ap-southeast-2`, `infra/app.py`). Models are called on
demand by bare model id, never through a cross-region inference profile, so the text and
pictures sent to Amazon Bedrock stay in Sydney (`infra/stacks/bedrock.py` refuses any profile
that could route elsewhere). Per AWS's Bedrock service terms, prompts and outputs are not stored
by the service or used to train models. The certificate for the site lives in `us-east-1`
because CloudFront requires it; no user data does.

## Your account

- **Email address and password**, held by Amazon Cognito (`infra/stacks/auth_stack.py`). The
  password is never seen by this product's code. The email is shown back to you on Account and
  is the only contact detail kept.
- **Your credit balance and plan** (`ENTITLEMENT` item, `backend/shared/db.py`), and, if you have
  subscribed, the Stripe subscription id. Card details never reach us: Stripe hosts the whole
  payment page, and the webhook events we keep are ids and amounts, expiring after 30 days
  (`EVENT_TTL_DAYS`).

There is no self-service account deletion yet. Sign out removes the session from the device;
the account and its items remain until they are removed by hand.

## A meditation from words

- **What you type** ("tired but wired", a destination) is stored on the job record
  (`mood_text` on the `JOB#<id>` item) so that the meditation can be revisited from your
  collection and its script regenerated on a replay. It is sent to the model with an instruction
  not to repeat personal details back in the script (`backend/functions/generate_script/`,
  `CLAUDE.md` constraint 7), and it is not written to logs — logs carry job ids, statuses and
  counts (`CLAUDE.md` constraint 7).
- **The narration audio** lives in a private bucket and is served through short-lived signed
  links (`CLAUDE.md` constraint 6). It expires **90 days** after it was made
  (`AUDIO_RETENTION_DAYS`, `infra/stacks/data_stack.py`); the job record itself stays, so a
  dreamscape can be re-generated later from its words.
- **Letting a dreamscape go** on the collection screen marks the job deleted; the audio is left to
  expire on the same schedule (no code deletes a user's object — constraint 9).

## A meditation from a picture

- **The picture you upload** is written to a private bucket under a key that belongs to your
  account only, with its type and size fixed by the upload form (constraint 9). It is kept for
  **365 days** and then removed by the bucket's lifecycle rule (`PICTURE_RETENTION_DAYS`) — not
  sooner, because a revisit re-samples it into the moving picture behind a replay.
- **What the model sees in it** becomes a handful of mood keywords and a one-line summary,
  stored on the picture record for 365 days (`PICTURE_ITEM_TTL_DAYS`) and then on the job. The
  vision prompt instructs the model not to describe people and not to transcribe any text in the
  picture (`backend/functions/describe_picture/`, constraint 7). Keywords and summaries are treated
  as your content: on your items, never in a log.

## The companion

The companion is a conversation that ends in a meditation. It is available on the Pro plan only.

- **What you say to it, and what it says back**, is kept as a transcript, one item per turn
  (`AGENT#<session>#T<n>`, `backend/shared/db.py`). The transcript is what lets a conversation
  resume after a reload or a dropped connection. It expires **30 days** after the session opened
  (`AGENT_SESSION_TTL_DAYS`, `backend/shared/models.py`); leaving a conversation ("Start over",
  or simply closing it) charges nothing and changes nothing about when it expires.
- **What it remembers about you** is a short list of things you told it about your meditations —
  "prefers slow narration", "no ocean sounds" — recorded by its `save_user_insight` tool
  (`backend/agent/tools/memory.py`). The prompt tells it to note preferences only, one short
  phrase each, never how you feel on a given day and never personal details
  (`backend/agent/prompt.py`). This list has **no expiry**: it is yours to keep or clear.
  *Account → What it remembers* shows every entry with the date it was noted, and *Forget
  everything* removes the whole list (`DELETE /agent/memory`). A cleared memory is gone, not
  hidden; the next conversation starts fresh.
- **What it can see.** In a conversation the model is given a summary of your earlier
  meditations — an excerpt of the words you typed or the picture's keywords, the duration, the
  date (`get_session_history`, `backend/agent/tools/history.py`),
  your memory list, and the transcript of the current session. Nothing from any other account.
- **What it cannot do.** It cannot start a meditation or spend a credit. It can only *propose*
  one; the meditation starts when you tap *Start the meditation*, and that tap is the only thing
  that uses a credit (`CLAUDE.md` constraint 10; `backend/agent_runner/turns.py`).
- **Where the words go.** The transcript and the memory are read into the model's prompt for your
  own sessions and nowhere else: not into the generation pipeline's execution record (the brief
  you confirmed is stored on the job like typed words), and not into logs, which hold session ids,
  turn numbers, tool names and counts (`CLAUDE.md` constraint 7). Usage is measured in tokens,
  not words.
- **If you are in crisis.** The companion is not a crisis service. When a message reads that way
  it replies with a fixed text — Lifeline on 13 11 14, Beyond Blue on 1300 22 4636, 000 in an
  emergency (`CRISIS_TEXT`, `backend/agent/prompt.py`) — and does not propose a meditation. That
  reply is the same for everyone and is not medical advice.
- **How much.** 30 conversations a calendar month per account (`AGENT_SESSIONS_PER_MONTH`);
  the counter that enforces it expires after 62 days (`AGENT_QUOTA_TTL_DAYS`).

## Summary of retention

| Data | Where | Kept |
|---|---|---|
| Email, password | Cognito | until the account is removed |
| Balance, plan, subscription id | `ENTITLEMENT` | until the account is removed |
| Stripe webhook event ids | `EVENT#…` | 30 days |
| Words you typed for a meditation | `JOB#…` (`mood_text`) | until the account is removed |
| Narration audio | audio bucket, `jobs/` | 90 days |
| Uploaded picture | audio bucket, `pictures/<you>/` | 365 days |
| Picture keywords and summary | `PICTURE#…`, then `JOB#…` | 365 days on the picture record |
| Companion transcript | `AGENT#…`, `AGENT#…#T…` | 30 days |
| Companion memory | `MEMORY` | until you clear it |
| Companion monthly counter | `AGENTQUOTA#…` | 62 days |

Third parties that see data: Amazon Web Services (everything above, in Sydney), Stripe (payment
and subscription, on Stripe's own pages), and the text-to-speech provider that voices the script
(Volcano Engine, with Amazon Polly as the fallback — `backend/shared/tts/`), which receives the
generated script and nothing else.
