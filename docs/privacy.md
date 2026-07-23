---
icon: lucide/shield-check
---

<!-- This page exists for iOS App Store submission requirements. Not included in sidebar nav. -->

# Privacy Policy

Last updated: 2026-07-14

This Privacy Policy explains how MindRoom and related MindRoom clients/services handle information.

## Scope

This policy applies to:

- MindRoom software running in local, self-hosted, or hosted installations
- MindRoom services and documentation operated by MindRoom
- MindRoom client applications (including the iOS app)

## Who Can Access Your Information

This policy distinguishes between the MindRoom software, the operator of a MindRoom installation, the MindRoom project maintainers, and external service providers.

- **MindRoom software** means the open-source software running in a local, self-hosted, or hosted installation.
- **Installation operator** means the person or organization that controls that installation, its machine or server, and its storage.
- **MindRoom project maintainers** means the people who develop and publish the open-source MindRoom project.
- **External service providers** include the Matrix homeserver, AI model provider, Google, and any other service that you or the installation operator configure.

For a local or self-hosted installation, the MindRoom project maintainers do not automatically receive or have access to its OAuth tokens, Google data, prompts, agent responses, local sessions, or locally stored files merely because the software is installed or used.

The installation operator and anyone with administrative or filesystem access to the machine or storage may be able to access locally stored credentials and data.
Application-level access is separately controlled by authenticated requester identity, agent authorization, room membership, and the configured credential scope.
Being signed in to the computer does not by itself grant access to MindRoom data, and an authorized Matrix user may interact with a locally running MindRoom installation without being signed in to that computer.

Project maintainers do not gain access to local installation data merely by being maintainers.
When the same people operate a MindRoom-hosted service that you choose, they can process the data available to that hosted component in their separate role as its service operator.
Project maintainers may also receive specific information that you deliberately send in a support request.
For example, a Matrix homeserver operator can access plaintext message content in unencrypted rooms, while end-to-end encrypted rooms protect message bodies from the homeserver but not message metadata.

## Information Needed to Operate the App

To provide messaging features, MindRoom and your selected homeserver process data that is required for delivery, sync, and account functionality:

- account identifiers (for example, Matrix user IDs)
- messages and files you choose to send through your configured homeserver
- room metadata (room names, avatars, membership)
- app configuration and local preferences stored on your device
- diagnostic information you choose to share with support

## Matrix and Homeservers

MindRoom is built on Matrix.

- Message content, media, and account data are primarily stored and processed by the Matrix homeserver you choose so chats can work.
- If you use a third-party or self-hosted homeserver, that server's privacy policy also applies.
- This is protocol-level message delivery/storage behavior, not hidden background monitoring.

## Open Source Transparency

MindRoom is open source.

- Source code is publicly available at `https://github.com/mindroom-ai`.
- The app and supporting services can be independently audited by anyone.

## iOS Permissions

The iOS app may request access to:

- **Microphone**: to record voice messages
- **Camera**: to capture photos/videos for chat attachments
- **Photo Library**: to select media to send
- **Photo Library Add Access**: to save media to your device
- **Local Network**: to connect to a local/self-hosted Matrix homeserver on your network

These permissions are only used to provide the corresponding app features.

## How We Use Information

We use information to:

- provide messaging and collaboration features
- deliver media upload/download and rendering features
- support account, authentication, and SSO flows
- respond to support requests
- improve reliability and fix bugs

## Sharing

We do not sell your personal information.

Information may be shared only as needed to:

- operate the service features you request (for example, with your configured Matrix homeserver)
- comply with legal obligations
- investigate abuse or security incidents

## Google API Services

The MindRoom software running in your selected installation accesses Google user data only after you connect a Google integration and grant the requested permissions.
Granting that access does not give the MindRoom project maintainers general access to your Google Account or automatically send them your OAuth tokens or Google data from a local or self-hosted installation.
When a paired local installation uses MindRoom's desktop OAuth client, the provisioning service sends the app client configuration to that installation and Google returns the authorization response directly to its loopback callback.
The local MindRoom process performs the token exchange with Google and stores the resulting tokens; the provisioning service does not receive the Google authorization code, tokens, or Google API data.
Control of the OAuth app registration lets the project maintainers manage or disable the client, but it does not by itself reveal a user's OAuth tokens or Google data to them.

Depending on the integrations you connect, this data can include your Google identity information, Gmail messages and metadata, Drive file metadata and contents, Calendar data, and Sheets spreadsheet values.

The MindRoom software uses this data only to provide the user-facing agent features that you request or configure, such as searching email, reading a Drive file, managing a calendar event, or reading and updating a spreadsheet.

Google connections follow the selected agent's credential scope:

- With `worker_scope: user`, the connection is isolated to the authenticated Matrix requester and can be used by that requester's user-scoped agents.
- With `worker_scope: user_agent`, the connection is isolated to the authenticated Matrix requester and the selected agent.
- With `worker_scope: shared`, the connection belongs to the selected shared agent, so any user authorized to invoke that agent can cause it to access the connected Google Account and may receive Google data in the agent's response.
- With no worker scope configured, the connection is stored at the installation level and is not isolated by requester.

Relevant Google data is sent to the AI model provider that you configure for inference so the agent can complete your request.

The MindRoom project does not use Google user data to train or improve generalized, foundational, or frontier AI models.

OAuth tokens are stored in the selected installation's MindRoom credential store, and Google data returned by a tool may be retained in its session storage and Matrix conversation history.

Your configured Matrix homeserver and AI model provider process data only as needed to provide the features you request and under their respective terms and privacy policies.

The MindRoom project does not sell Google user data, use it for advertising, use it to determine creditworthiness, or transfer it except as needed to provide the features you request, comply with law, or protect security.

You can stop future Google API access by disconnecting the integration in MindRoom or revoking MindRoom from your Google Account permissions, and you can delete locally retained data by deleting the relevant MindRoom sessions, Matrix messages, or local storage.

The MindRoom software's use and transfer of information received from Google APIs adheres to the [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy), including the Limited Use requirements.

## Data Retention

Retention depends on the system component:

- data stored on Matrix homeservers is retained according to the homeserver operator's policies
- local app data remains on your device until you remove it or delete the app
- support emails and diagnostics may be retained for support and security purposes

## Account Deactivation / Deletion

The MindRoom iOS app provides an in-app account deactivation path:

- `Settings` -> `Account` -> `Delete / Deactivate Account`

Actual deletion/deactivation behavior depends on the capabilities and policies of your Matrix homeserver.

## Security

We use reasonable measures to protect information, but no system is completely secure.

## Children's Privacy

MindRoom is not intended for children under 13 (or the minimum age required in your jurisdiction) without appropriate supervision and authorization.

## Changes to This Policy

We may update this policy from time to time. The "Last updated" date will change when material updates are published.

## Contact

For privacy questions, contact:

- `support@mindroom.chat`
