---
icon: lucide/smile-plus
---

# Custom Emoji

A custom emoji that someone adds in MindRoom Chat is saved to their own account, so only they can use it.
Everyone else sees the picture in messages and reactions but cannot pick it.
Sharing a whole pack by hand means every person has to find it and turn it on for themselves.

Server custom emoji remove those steps.
Someone adds an emoji once.
Everyone with an account on the same homeserver can use it right away.
It appears in the reaction picker, the composer's emoji board, and `:` autocomplete, in every room including direct messages, with nothing to set up.

The emoji belong to one homeserver.
People on other homeservers cannot pick them, but MindRoom Chat shows them the picture in messages and reactions.

## How it works

1. The homeserver's `/.well-known/matrix/client` file names one room with the key `io.mindroom.emoji`.
2. After its first sync, MindRoom Chat joins that room by itself, so nobody accepts an invite.
3. MindRoom Chat hides the room from room lists, search, and room mentions.
4. The room's emoji packs appear in the emoji board and `:` autocomplete of every room, next to that room's own packs and the user's personal one.
5. To add an emoji, a user presses **+** (**Add custom emoji**) in the emoji board, picks an image, gives it a shortcode, and presses **Add**.
   An image larger than 128 pixels is shrunk to 128 pixels before upload, and a GIF is kept as it is so it keeps animating, up to 256 KB.
   A shortcode that is already taken is refused with "That name is taken. Pick another."
6. When the user may add to the room, MindRoom Chat saves the emoji to its pack, and each member's next sync delivers it.

The room is an ordinary Matrix room, and its packs are ordinary [MSC2545](https://github.com/matrix-org/matrix-spec-proposals/pull/2545) image packs (`im.ponies.room_emotes` state events).
A client that supports image packs can join the room and use them like the packs of any room.

## Enable it

Create the room once, with an operator's access token, from the root of a MindRoom checkout:

```bash
MATRIX_ACCESS_TOKEN=... uv run python -m scripts.utilities.create_server_emoji_room \
  --homeserver https://matrix.example.com --name "Example"
```

The script creates a room of the type `io.mindroom.emoji`, which is what makes MindRoom Chat hide it.
The room does not federate, local users can join it without an invite, and it stays out of the public room directory.
It holds one empty emoji pack named after `--name`, and the account behind the token owns it.

| Flag | Meaning |
| --- | --- |
| `--homeserver` | Base URL of the homeserver. Required. |
| `--name` | Name of the room and of its first emoji pack. Required. |
| `--alias` | Local part of the room alias, `emoji` by default. The full alias is `#<alias>:<server name>`, with the server name of the account behind the token. |
| `--moderators-only` | Let only moderators add, rename, or remove emoji. |

The script prints the room ID and the key that names the room:

```text
Room ID: !sXYnTpwnpR9fs3ppoN2Y4nSUwVEGenUhG2FCZ8W5ALs
Add to /.well-known/matrix/client: {"io.mindroom.emoji": {"room": "#emoji:example.com"}}
```

Add that key to the homeserver's `/.well-known/matrix/client` file:

```json
{
  "m.homeserver": { "base_url": "https://matrix.example.com" },
  "io.mindroom.emoji": { "room": "#emoji:example.com" }
}
```

`room` takes the alias the script printed, or the room ID.
With the `mindroom-client` Helm chart, set `serverEmoji.room` to the same value and the chart serves `/.well-known/matrix/client` with the key; see the [chart README](https://github.com/mindroom-ai/mindroom/blob/main/cluster/k8s/client/README.md#server-custom-emoji).
Quote the value in the values file, as in `room: "#emoji:example.com"`: unquoted, `#` starts a YAML comment and `!` a YAML tag, and the chart then publishes nothing.
Tuwunel's own `[global.well_known]` section has no setting for extra keys, so when Tuwunel answers that path, serve the file from your reverse proxy or the client chart instead.
Nothing changes in `config.yaml`.
Without the key, custom emoji stay personal to each user.

## If the script stops early

When a call fails, the script prints `Room creation stopped:` and the reason, such as Matrix's own error, and exits with status 1.
`M_ROOM_IN_USE` means the alias is taken, so pick another `--alias`.

If the script stops after it created the room, for example while setting the power levels, the room stays with the server's default levels, where only moderators may add emoji and every member may post messages, and no key is printed.
Publish `#<alias>:<server name>` as the key yourself after you [set who may add emoji](#who-may-add-emoji) and [stop members from posting](#moderate-the-room), or run the script again with another `--alias`.

## Check that it works

MindRoom Chat reads the well-known file when it loads, so open tabs pick up a new key on reload.
Open the emoji board in any room, press **+**, and pick an image.
The form says where the emoji will go.

- "Everyone on example.com can use it in every room." means MindRoom Chat found the room and the user may add to it.
- "It is saved to your Personal Pack, so you can use it in every room." means the emoji stays personal.
  The key is missing or malformed, MindRoom Chat could not join the room, or the user may not add.

A failed join is logged once in the browser console as `[server-emoji] could not join the server emoji room`, and it is not tried again until the page reloads.

## Who may add emoji

The room's power level for `im.ponies.room_emotes` decides who may add emoji.
The script sets it to `0` so every member may add, and to `50` with `--moderators-only` so only moderators may.
The same level lets someone rename or remove emoji, because a pack is a single Matrix state event.

A user who may not add still sees **+**.
Their emoji goes to their own Personal Pack, and the form says so.
To change the rule later, set **Manage Emojis & Stickers** in the room's permissions.

In MindRoom Chat, the **Server Emoji** section of **Settings → Emojis & Stickers** lists the server's packs.
Opening a pack shows its emoji.
Users who may change the pack can rename or delete them there.
Users who may change power levels also get a **Permissions** button that opens the room's permissions.
Simple Mode (**Settings → General → Interface**) hides the Emojis & Stickers page, so turn it off to manage the packs.

## Moderate the room

Members cannot post messages in the room.
A message there would notify everyone on the server, and MindRoom Chat gives users no way to mute a room it hides.
The script raises the room's `events_default` power level to at least `50` for this, and any explicit `m.room.message` or `m.room.encrypted` level below that.
If you create the room by hand, raise it too.
MindRoom Chat also mutes the room when it joins it, so a mention hidden in a pack event notifies no one.

- A kick does not last, because MindRoom Chat joins the room again by itself.
  Ban someone to keep them out.
- At the default power level `0`, any member may also create more packs, each up to about 64 KB that every member syncs.
  A server with open registration should use `--moderators-only`.
- A room made by hand needs the published alias as its canonical alias, because MindRoom Chat finds the joined room by it.
- To replace the room, tombstone the old one or remove its canonical alias, then publish the new one.

## Limits

- Two people adding to the same pack at the same moment can lose one emoji, because the last write wins.
  MindRoom Chat reads the pack right before it writes, so the window is short.
  The person whose emoji vanished adds it again.
- Whoever may add may also rename or remove any server emoji.
  Limit adding to moderators if that is too open.
- A pack is one Matrix event, which has a size limit.
  When a pack is full, MindRoom Chat opens another pack in the same room with the same name.
- Leaving the room does not opt a user out, because MindRoom Chat joins it again.
- Every account that runs MindRoom Chat becomes a member of the room, so its member list names all of them.
  Only MindRoom Chat hides the room, so other clients logged in to the same account list it like any other room.
- Other Matrix clients do not join the room or add to its packs by themselves.
- Custom emoji a user picks appear under Recent, in the `:` autocomplete list and in the quick reactions, ranked by use like Unicode emoji.
- The shrinking, the GIF limit and the taken-name check apply to **Add custom emoji** in the emoji board; the pack editor under **Settings → Emojis & Stickers**, which also serves sticker packs, uploads images as they are and numbers duplicate names.
- Emoji show no "added by", and existing personal emoji do not move to the server, so add them again to share them.
