# How to add the War Intel Bot to your Discord server

Easy steps. Do them in order. Takes about 10 minutes.

## What you need first

- You must be a **boss of the Discord server**. That means you have the
  "Manage Server" permission.
- Your **Torn faction ID**. It's a number.
- A **Torn API key**. It's like a password the bot uses to read Torn.

---

## Step 1: Invite the bot

1. Click this link:
   https://discord.com/api/oauth2/authorize?client_id=<BOT_CLIENT_ID>&permissions=117776&scope=bot%20applications.commands
2. A Discord page opens. Pick **your server** from the list.
3. Click **Continue**.
4. It shows a list of things the bot can do. Leave every box ticked.
5. Click **Authorize**.
6. Now the bot is in your server. It also got its own role, called **War Intel Bot**.

## Step 2: Find your faction ID

1. Open Torn and go to **your faction's page**.
2. Look at the web address at the top.
3. Find the part that says `ID=` followed by a number. That number is your faction ID.
   Write it down.

## Step 3: Make a Torn API key

1. In Torn, go to **Preferences** and then **API Keys**.
2. Make a new key and set it to **Limited Access**.
3. Copy the key.
4. Keep it secret, like a password. Don't paste it in a normal chat.

## Step 4: Tell the bot who you are

Type these in **any channel** in your server. The bot's replies are secret,
so only you see them, and your key stays safe.

1. Type `/setup faction`, put your faction ID in the box, and press Enter.
2. Type `/setup torn-key`, paste your key in the box, and press Enter.
   - If the bot says **"Torn key saved"**, it worked. 👍
   - If it says **"Torn key rejected"**, the key is wrong. Make a new one and try again.
3. **Optional:** if you have an FFScouter key, type `/setup ffscouter-key`, paste it,
   and press Enter. This helps the bot guess how strong enemies are.
4. Type `/setup show`. It shows what you saved, with your keys hidden. Check that it looks right.

## Step 5: Wait for a war

- **Right now nothing will appear, and that's normal.**
- When Torn announces your next ranked war, the bot wakes up by itself. It happens within 1 minute.
- It makes a new group of channels called **⚔️ WAR — (enemy name)** with 5 channels inside:
  - war information
  - enemy stats
  - target board
  - war log
  - enemy activity
- It keeps filling them in by itself during the war.

## Step 6 (optional): Make the war channels read-only

This lets people read the war channels but stops them typing in them.
Do it once, after the channels appear. The bot uses the same channels
for every war after that.

1. Right-click the **⚔️ WAR** group name and click **Edit Category**.
2. Click **Permissions**.
3. Click **@everyone**.
4. Set **Send Messages** to the red **✗**.
5. Click **Save Changes**.

---

## Another way: make the channels yourself (bot needs no "Manage Channels")

Use this if you don't want the bot to be able to make or change channels.
You make the 5 channels, and the bot only posts in them.

1. Invite the bot with **this** link instead of the Step 1 link (it doesn't ask for
   "Manage Channels"):
   https://discord.com/api/oauth2/authorize?client_id=<BOT_CLIENT_ID>&permissions=117760&scope=bot%20applications.commands
   - Bot already in your server? Go to **Server Settings → Roles → War Intel Bot**
     and switch **Manage Channels** off. That's all.
2. Make 5 text channels, named:
   `war-information`, `enemy-stats`, `target-board`, `war-log`, `enemy-activity`
3. In each channel, let the **War Intel Bot** role in: **View Channel, Send Messages,
   Embed Links, Read Message History** (and **Attach Files** in `war-log` for the
   end-of-war spreadsheet).
4. Go into each channel and type `/setup board`, then press Enter.
   - The bot works out which board goes there from the channel's name.
   - Different name? Pick the board in the `board` box.
   - It tells you which boards still need a channel.
5. Type `/setup show` to check all 5 are set.

The same channels are used for every war. To stop a board posting somewhere, run
`/setup board` in that channel with `remove` set to True.

---

## ⚠️ Don't do this (unless you know why)

**Don't use `/setup channel`.** It puts everything into one channel instead of
5 channels, which gets messy.

If you really want it, the bot must be allowed in that channel first:

1. Open the channel's **⚙️ settings** and click **Permissions**.
2. Click **+** and add the **War Intel Bot** role.
3. Give it the green ✓ for **View Channel, Send Messages, Embed Links,
   Read Message History**, then save.

To go back to 5 channels, type `/setup channel` with the box left empty.

---

## Something wrong?

| What you see | What to do |
|---|---|
| No `/setup` commands when you type `/` | Kick the bot and invite it again with the Step 1 link. |
| "You do not have permission to use that command." | You need "Manage Server". Ask a server boss to do the setup. |
| No war channels yet | Is a war announced? If not, it's normal. Wait. |
| Channels are there but empty | The bot may be blocked. Check the bot is allowed in those channels (see the ⚠️ box above). |
| To turn the bot off | Type `/setup disable`. |
