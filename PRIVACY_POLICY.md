# Privacy Policy — Torn War Intelligence Bot

_Last updated: 2026-09-10_

This policy explains what the Torn War Intelligence Bot ("the bot") stores, why,
and who it is shared with. The bot is operated by the individual or group that
hosts it ("the operator"). If you were invited to use a hosted instance, the
operator is your point of contact for any data request.

## 1. What the bot is

A Discord bot for Torn City factions. When a server configures it with `/setup`,
the bot watches that faction's ranked wars via the Torn API, pulls the opposing
faction's public roster, optionally enriches it with battle-strength **estimates**
from FFScouter, and presents the result in Discord.

The bot only makes outbound API calls. It does **not** read your Discord
messages (it does not request the Message Content or Server Members privileged
intents) and it never performs any action inside Torn.

## 2. What it stores

Per Discord server that has run `/setup`:

| Data | Purpose |
|---|---|
| Discord server (guild) ID | Identify which server a configuration and its wars belong to |
| Discord user ID of the person who ran `/setup` | Audit — who configured the server |
| Your Torn faction ID | Know which faction to monitor |
| Your Torn API key | Call the Torn API on your faction's behalf. **Stored encrypted at rest** (Fernet / AES); never logged |
| Your FFScouter API key (if provided) | Call FFScouter for estimates. **Stored encrypted at rest**; never logged |
| An optional role ID | Restrict who can use war-intel commands |
| IDs of Discord categories, channels and messages the bot created | Update the live war embeds in place instead of reposting |

Per war the bot detects:

| Data | Purpose |
|---|---|
| Torn war ID, both factions' IDs, names, scores, start/end/result | War tracking and history |
| Opposing players' Torn IDs, names, levels, faction and in-game status (okay / hospital / jail / travelling) | Building the enemy roster. This is public information already visible on the Torn API |
| Battle-strength estimates and their history (from FFScouter), fair-fight values | Showing strength ranking and trend. Always labelled as **estimated**, never presented as confirmed stats |
| Any confirmed stats a faction member manually enters (future `/spy` feature) | Kept separate from estimates; used only by that faction |

The bot does **not** store Discord message content, member lists, email
addresses, IP addresses, or payment information. There is no payment system.

## 3. Where data goes

The bot sends data to two third-party services, only as needed to do its job:

- **Torn API** (`api.torn.com`) — receives your configured Torn API key and
  faction / player IDs; returns public Torn game data. Governed by Torn's own
  terms and privacy policy.
- **FFScouter** (`ffscouter.com`) — receives your configured FFScouter key and
  opposing players' Torn IDs; returns battle-strength estimates. Governed by
  FFScouter's own terms and privacy policy.

Data is **not** sold, rented, or shared with anyone else. It is not used for
advertising or profiling.

## 4. Retention and deletion

- Configuration and war data persist in the operator's database while your
  server is set up.
- Running **`/setup disable`** stops all processing for your server. Ask the
  operator to fully delete your stored configuration (including the encrypted
  keys) and your war history if you want it removed entirely.
- Historical enemy estimates may be retained in aggregate to improve the bot's
  own future estimator, but can be deleted on request.
- If the operator loses or rotates the encryption key, all stored API keys
  become unreadable and every server must re-run `/setup`.

## 5. Security

- API keys are encrypted at rest and are never written to logs or shown in
  Discord (command replies that involve a key are ephemeral / private).
- The bot requests only the Discord permissions it needs (View Channels, Send
  Messages, Embed Links, Read Message History, Manage Channels). It never
  requests Administrator.
- You control which Torn key you provide. A **Limited Access** key is
  sufficient; do not provide a Full Access key.

## 6. Your choices

- Don't want your faction's data processed? Don't run `/setup`, or run
  `/setup disable`.
- Want your data deleted? Contact the operator of the instance you use.
- You can revoke the bot's Torn API access at any time from
  <https://www.torn.com/preferences.php#tab=api>.

## 7. Changes

This policy may be updated. Material changes will be noted in the project's
changelog / release notes.

## 8. Contact

Contact the operator of the bot instance you were invited to. For the software
itself, use the project's issue tracker.
