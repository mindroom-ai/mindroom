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

The app cannot hold the [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/access-controls/) cookie, so a homeserver behind Access redirects the app's requests to the Access login and sign-in fails.
When the homeserver advertises a handoff page, the app instead sends the Access token in a `cf-access-token` header, which Access accepts.

With the steps below in place, the sign-in screen asks for the organization's sign-in first, then shows the usual sign-in options.
When the Access session ends, the bar at the top asks for that sign-in again.
An Access session revoked before it expires shows as a lost connection until the token's expiry time.
Calls need `/.well-known/matrix/client`, which these steps leave behind Access, so calls do not work on iOS for such a homeserver.
The sign-in sheet shares Safari's cookies, so if it ends on Cloudflare's "Invalid session" page, delete the website data for `cloudflareaccess.com` and the homeserver's domain, then sign in again.
That data is under Settings › Apps › Safari › Advanced › Website Data, or Settings › Safari › Advanced › Website Data on iOS 17 and earlier.
Push notifications for such a homeserver carry no message content and read "New message".
The public push gateway still receives the event and room IDs, the unread count and the device's push token, and each push tells it that the homeserver is active; the advert can turn push off instead.

Set up the homeserver (examples use `chat.example.com`):

1. Publish the advert at `https://chat.example.com/.well-known/mindroom/cloudflare-access`, outside Access, as JSON with `Access-Control-Allow-Origin: *`:
   `{"handoff_url": "https://chat.example.com/native/cloudflare-access"}`.
   The handoff URL must use HTTPS on the homeserver's own origin.
   Add `"push": false` to turn push off: the app then registers no pusher for the homeserver and hides the push setting.
   Signed-in accounts pick up a change of the advert only when they sign out and in again.
2. Serve the handoff page behind Access. It redirects to the app with the value of the `CF_Authorization` cookie:

    ```nginx
    location = /native/cloudflare-access {
      add_header Cache-Control "no-store" always;
      return 302 "mindroom://cloudflare-access#token=$cookie_CF_Authorization";
    }
    ```

   If the same origin serves the MindRoom Chat web app, exclude `/native` from its service worker's navigation fallback (`serviceWorker.navigationFallbackExcludePaths` in the client chart, or `window.__SERVICE_WORKER_NAVIGATION_FALLBACK_EXCLUDE_PATHS__`), or a Safari that has opened the web app answers the handoff with its own page.
   The page also answers when opened in Safari, where iOS hands the token to whichever app handles `mindroom://`.
   That token gets past Access for every host of the Access application until the session ends, so give the homeserver its own Access application.
3. Turn on "Bypass OPTIONS requests to origin" (`options_preflight_bypass`) on the Access application, because browsers send CORS preflights without credentials.
   Every `OPTIONS` request then reaches the origin without an Access session, so make sure no route answers `OPTIONS` with private content; nginx `return` answers any method.
   Leave the application's binding cookie off, because the app sends only the token.
4. Let the advert path, `/_matrix/client/versions`, `/_matrix/client/v1/media` and `/_matrix/media/v3/upload` bypass Access.
   The app checks `/versions` before the organization's sign-in, images and uploads cannot carry the header, and the homeserver refuses the media paths without a Matrix access token.
   A bypass covers everything below its path, so answer 404 for anything below the advert path.
   Cloudflare also matches a bypass before nginx decodes `%2F` and resolves `..`, so `/.well-known/mindroom/cloudflare-access/..%2F..%2Fsecret` would reach `/secret` without Access.
   Reject such requests in the nginx `server` block, before any `location`; the check looks only at the path, so encoded URLs in query strings still pass:

    ```nginx
    if ($request_uri ~* "^[^?#]*(?:%2f|%5c|%2e|/\.\.?(?:[/?#]|$))") { return 404; }
    ```

   If `/_matrix` also passes through this nginx, the check refuses Matrix requests that encode `/` in a path parameter, such as some state keys, so send `/_matrix` to the homeserver without this nginx where you can.
5. Run a homeserver that allows the `cf-access-token` header in CORS, such as the MindRoom fork of Tuwunel (`v1.9.4-mindroom.18` or later).

Before moving an existing homeserver behind Access, delete its pushers for the app (app ID `chat.mindroom.app`) that point at the public push gateway, for example with Tuwunel's `!admin query pusher get-pushers <user>` and `!admin query pusher remove-pusher <user> <pushkey>`.
Those pushers send full message content, and an account signed in before the move cannot remove them, because its sign-out cannot pass Access.
Such accounts need to sign out and in again.
To take a homeserver off Access later, keep answering the advert path with a 404 and `Access-Control-Allow-Origin: *`, and have iOS accounts sign out and in again; until then the app keeps waiting for an Access sign-in that can no longer finish.

## Reporting Issues

Report problems through the [support page](support.md#what-to-include-in-a-support-request), which lists the device, app, and homeserver details to include.
