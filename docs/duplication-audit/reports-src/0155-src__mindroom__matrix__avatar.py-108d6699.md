# Summary

Top duplication candidate: Matrix upload helpers repeat the same in-memory byte upload pattern and `nio.UploadResponse` validation in `src/mindroom/matrix/avatar.py`, `src/mindroom/matrix/client_delivery.py`, and `src/mindroom/matrix/large_messages.py`.
The avatar-specific user and room profile/state setters are mostly unique, with only related room-state response handling elsewhere.

# Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_guess_avatar_content_type	function	lines 14-28	related-only	mimetypes.guess_type image content type suffix fallback _guess_mimetype	src/mindroom/matrix/client_delivery.py:266-268; src/mindroom/attachments.py:172
_upload_avatar_file	async_function	lines 31-83	duplicate-found	Matrix client.upload data_provider UploadResponse content_uri file bytes filesize content_type	src/mindroom/matrix/client_delivery.py:271-342; src/mindroom/matrix/large_messages.py:293-387
_upload_avatar_file.<locals>.data_provider	nested_function	lines 56-57	duplicate-found	data_provider BytesIO upload payload monitor data closure	src/mindroom/matrix/client_delivery.py:317-318; src/mindroom/matrix/large_messages.py:353-354
_set_avatar_from_file	async_function	lines 86-111	none-found	client.set_avatar ProfileSetAvatarResponse user_avatar_set	src/mindroom/matrix/avatar.py:100-110; src/mindroom/bot.py:885-895
check_and_set_avatar	async_function	lines 114-140	related-only	get_profile avatar_url room_has_avatar set_room_avatar_from_file check and set avatar	src/mindroom/matrix/rooms.py:45-78; src/mindroom/avatar_generation.py:353-370
set_room_avatar_from_file	async_function	lines 143-175	related-only	room_put_state m.room.avatar RoomPutStateResponse content url avatar	src/mindroom/topic_generator.py:160-170; src/mindroom/commands/config_confirmation.py:153-175; src/mindroom/hooks/state.py:56-64; src/mindroom/avatar_generation.py:353-370
room_has_avatar	async_function	lines 178-184	none-found	room_get_state_event m.room.avatar content url avatar already set	src/mindroom/avatar_generation.py:362; src/mindroom/matrix/avatar.py:178-184
```

# Findings

## Matrix byte upload plumbing is duplicated

`src/mindroom/matrix/avatar.py:45-83` reads a local file, builds an `io.BytesIO` `data_provider`, calls `client.upload`, accepts both tuple and direct `UploadResponse` return shapes, validates `content_uri`, logs failures, and returns the MXC URI.
`src/mindroom/matrix/client_delivery.py:271-342` performs the same core upload behavior for local message attachments after optional room encryption.
`src/mindroom/matrix/large_messages.py:293-387` performs the same core upload behavior for generated text sidecars after optional room encryption.

The duplicated behavior is the Matrix upload wrapper around bytes: provide a BytesIO stream, pass content type/name/size, normalize nio's upload response shape, require `nio.UploadResponse.content_uri`, and return an MXC URI or failure.
Differences to preserve: avatar uploads do not encrypt and return only `str | None`; file delivery and large-message sidecars may encrypt payloads and return Matrix file metadata alongside the URI; log event names differ by feature.

## MIME type guessing is related but not the same

`src/mindroom/matrix/avatar.py:14-28` and `src/mindroom/matrix/client_delivery.py:266-268` both use `mimetypes.guess_type(path.name)` with an `application/octet-stream` fallback.
The avatar helper adds image-only validation and explicit image suffix fallbacks, while the message attachment helper accepts any guessed MIME type.
This is related content-type inference, but not enough by itself to justify a shared helper unless the upload helper also centralizes MIME handling.

## Avatar state/profile application is mostly unique

`src/mindroom/matrix/avatar.py:86-140` handles user avatar presence and setting through `get_profile` and `set_avatar`.
No other source file duplicates the user profile avatar flow.
`src/mindroom/matrix/rooms.py:45-78` and `src/mindroom/avatar_generation.py:353-370` call the avatar helpers and add caller-specific logging or console output, but they do not duplicate the profile/set-avatar implementation.

`src/mindroom/matrix/avatar.py:143-175` uses `room_put_state` for `m.room.avatar`.
Other room-state writes exist in `src/mindroom/topic_generator.py:160-170`, `src/mindroom/commands/config_confirmation.py:153-175`, and `src/mindroom/hooks/state.py:56-64`, but they set different event types and have different error contracts.
This is a common Matrix state-write pattern rather than direct duplicated avatar behavior.

# Proposed generalization

Add a small Matrix media upload helper only if upload plumbing changes are already being touched.
A minimal candidate location is `src/mindroom/matrix/media_upload.py` or an existing Matrix media module, with a function that accepts `client`, `payload: bytes`, `content_type`, `filename`, `log_context`, and returns `str | None` after normalizing tuple/direct nio responses and validating `content_uri`.

No refactor is recommended for the user avatar, room avatar, or room-avatar-presence functions.
They are concise, domain-specific, and already serve as the shared API used by room creation and managed avatar sync.

# Risk/tests

Behavior risks for an upload-helper refactor would be response-shape handling, preserving current logging context, and not weakening encrypted upload metadata handling in message delivery and large-message sidecars.
Tests should cover tuple and direct `client.upload` responses, upload errors, missing `content_uri`, and a successful avatar upload followed by both `set_avatar` and `room_put_state`.
No production code was changed for this audit.
