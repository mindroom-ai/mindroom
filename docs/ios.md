---
icon: simple/apple
---

# iOS App

MindRoom AI is the MindRoom Chat client for iPhone, iPad, and Apple silicon Macs, available on the App Store.
It requires iOS or iPadOS 16.4 or later, or macOS 13.3 or later on a Mac with an M1 chip or newer.

<p>
  <a class="md-button md-button--primary" href="https://apps.apple.com/us/app/mindroom-ai/id6760272172">Download on the App Store</a>
</p>

## Sign In

The app is a Matrix client, so sign in with the Matrix homeserver and account you normally use.
For hosted MindRoom, the homeserver is `https://mindroom.chat`.

To pair a local MindRoom runtime with that account, follow [Getting Started](getting-started.md#2-approve-pairing); on a computer, approve the pairing at `https://chat.mindroom.chat`.

## Homeservers Behind Cloudflare Access

A homeserver behind [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/access-controls/) redirects the app's requests to the Access login, because the app cannot hold the Access cookie, so sign-in fails.
The app can instead send the Access token in a `cf-access-token` header, which Access accepts, when the homeserver advertises a handoff page.

With the steps below in place, the sign-in screen asks for the organization's sign-in first, then shows the usual sign-in options.
When the Access session ends, the bar at the top asks for that sign-in again.
Push notifications for such a homeserver carry no message content: the public push gateway sees only event and room IDs and the unread count, and the notification reads "New message".

Set up the homeserver (examples use `chat.example.com`):

1. Publish the advert at `https://chat.example.com/.well-known/mindroom/cloudflare-access`, outside Access, as JSON with `Access-Control-Allow-Origin: *`:
   `{"handoff_url": "https://chat.example.com/native/cloudflare-access"}`.
   The handoff URL must use HTTPS on the homeserver's own origin.
2. Serve the handoff page behind Access. It redirects to the app with the value of the `CF_Authorization` cookie:

    ```nginx
    location = /native/cloudflare-access {
      add_header Cache-Control "no-store" always;
      return 302 "mindroom://cloudflare-access#token=$cookie_CF_Authorization";
    }
    ```

   The page also answers when opened in Safari, where iOS hands the token to whichever app handles `mindroom://`; the token only gets past Access for this host, until the Access session ends.
3. Turn on "Bypass OPTIONS requests to origin" (`options_preflight_bypass`) on the Access application, because browsers send CORS preflights without credentials.
   Every `OPTIONS` request then reaches the origin without an Access session, so make sure no route answers `OPTIONS` with private content; nginx `return` answers any method.
4. Let `/_matrix/client/versions`, `/_matrix/client/v1/media` and `/_matrix/media/v3/upload` bypass Access.
   The app checks `/versions` before the organization's sign-in, images and uploads cannot carry the header, and the homeserver refuses the media paths without a Matrix access token.
5. Run a homeserver that allows the `cf-access-token` header in CORS, such as the MindRoom fork of Tuwunel.

Access application paths include everything below them, so answer 404 for anything under the public advert path.
Before moving an existing homeserver behind Access, delete its pushers for the app (app ID `chat.mindroom.app`) that point at the public push gateway, for example with the homeserver's admin tools.
Registrations made before the move send full message content, and the app does not replace them.
Accounts that were signed in before the move need to sign out and in again.

## Reporting Issues

Report problems through the [support page](support.md#what-to-include-in-a-support-request), which lists the device, app, and homeserver details to include.
