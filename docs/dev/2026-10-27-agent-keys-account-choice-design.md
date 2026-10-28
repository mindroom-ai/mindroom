# Choosing the Account of an Agent Keys Service

This record explains how a user gives an agent a second account of a login provider (for example a second Gmail account) on the Agent Keys page.

## Why

A service of the user's own can sign in with an account of its own (`oauth_separate_account`), which is how an agent gets a second account of a provider beside the one its tools use, such as a shared mailbox next to the user's own Gmail.
A user adding such a service looks at the main form and at the row of the service they already have, so the choice belongs in both places, not among advanced options.
A login service never uses an API key, so a key field in its form only invites a wrong answer.
A user who adds Gmail next to a Gmail service that already uses the tools' account almost always wants another account.
Each warning beside the form costs attention, so the form warns only where two services can really take the same request.

## Walkthrough

For a user whose Mind already has Gmail from their administrator, signed in as their own address.

From the row:

1. On the Agent Keys page, the Gmail row shows the user's address and a link, **Add another Gmail account**.
2. They click it. A new Gmail row appears under **Added by you**, and a box, "Sign in to another Gmail account?", asks what agents may do with the new account: **Read only** (picked) or **Read and write**.
3. They click **Continue**. Google's sign-in opens and they pick the other account.
4. The new row shows that account's address and "Using connected account, Read only". Agents use it by naming it in a request header, which the row shows in small print.

From **Add service**:

1. They click **Add service** and pick Gmail. The form asks **Account**: "The account your tools use (their address)" or "Another Gmail account". The second is already picked, because the Gmail row uses the first; a line under the first says so.
2. They click **Save and sign in**. Steps 2 to 4 above follow.

## Decisions

- **The form asks which account, in place of the API key.**
  For a personal service whose login is not requester-scoped, on an agent that is not shared (`offersSeparateAccount`), the form shows a field **Account** with two choices:
  "The account your tools use", followed by that account's address when another service of the agent shows it, saves `oauth_separate_account: false`;
  "Another {provider} account", with the hint "You sign in right after saving. Agents choose it with `X-Agent-Keys-Service: {name}`" (the header as a code chip, as on the row; the first sentence only where saving signs in next), saves `true`.
  For a preset the field sits in the main form, right after Preset; for a custom service it sits in **More options**, right after the login select it depends on.
  A new service starts on "Another" when another service of the agent (not a limit) already uses the same provider without its own account, and on "The account your tools use" otherwise, decided again whenever its preset or login changes; while such a service exists, "The {that service} row already uses this account." shows under the first choice.
  Editing shows the saved choice; switching an own account back to the tools' account removes it on save.
- **No API key in that form.** The API key field and the lines that offer a key instead of an account ("Users can connect an account (…) instead of an API key.") are not shown while the Account field is. A service on the tools' account can still take a key from its row (**Use an API key instead**). Administrators' forms and shared agents show the API key field and no Account field.
- **Saving an own account that is not signed in signs in next.** When the saved service will have its own account and had none for the same login (a new service, a switch from the tools' account, or a move to another login, whose old account the save removes), the save button reads **Save and sign in**; any other save reads **Save service**. After the save succeeds, the editor closes, and once the reloaded listing shows the service with that login, its row opens the allow dialog, titled "Sign in to another {provider} account?", with the level choice where read only is offered, starting on Read only; **Continue** opens the provider's sign-in. The row as it was before the save, which still shows until the listing reloads, never asks. Cancelling leaves the row unconnected with its **Connect** button, and a row that cannot connect, such as one that needs a reset, asks nothing.
- **The row's way in is a link.** A row on the tools' account with a connected account, of a login whose accounts are not each user's own, on a personal agent the user manages, shows **Add another {provider} account** under its status; its menu has no such item. Without a connected account there is no first account for "another" to follow, so the row offers no link. A click saves the new service at once, through the same route the editor uses: the provider's preset, `oauth_separate_account: true`, the first free name of `{preset}`, `{preset}-2`, … among the agent's active and inactive services, and no display name. The new row then opens its allow dialog as above. A failed save shows the error and creates nothing.
- **Warnings are about real overlaps, said once.** A service warns about another only where a rule of each sends a credential to the same host and port (a missing port matches any) and one path prefix covers the other, by segment (an empty prefix covers the whole host). The warning reads "{other} already handles {addresses} for this agent, so requests there may use it instead of this service.", listing each shared address once. A service with its own account gets no such warning, since its requests name it; where it overlaps another service it shows one muted line, "Requests that name no service may use this account too.", because a request without the header follows the usual routing order, which may pick it.
- **Placeholder rules only when placeholders exist.** The paragraph of placeholder naming rules shows only while the list has an entry; the one-paragraph explanation and **Add placeholder** stay.
- **The row says what agents can do.** A connected account that agents may not use yet has the status "Not used by agents yet"; its button and the allow dialog's confirm read **Let agents use it**; the note under the row reads "Agents' requests are refused until you let them use this account."

## Later, not now

- Naming an own-account row after its address once signed in.
- Removing an own-account service whose sign-in was cancelled.
- Moving the Name field out of the main form.

## Assumptions

- No backend or API change: the PUT service route, the opt-in route and the connect flow already do everything above.
- The provider's display name reads naturally in "Add another {provider} account" (Gmail, Google Calendar, Google Drive).

## Testing

- Overlap: Gmail against Drive, Calendar and Tasks presets warns nothing; Gmail against Gmail warns once with both addresses; a whole-host rule overlaps every path on its host; different ports do not overlap.
- Editor: a Gmail preset shows Account and no API key field; the default follows the rule above; each choice saves the matching `oauth_separate_account`; the button reads **Save and sign in** only where the save signs in next; the custom-service path has the choice in More options; administrator and shared-agent forms show the key field and no Account field; an own account that overlaps shows the one muted line and no warning.
- After **Save and sign in** and after the row link, the new row's allow dialog is open, and **Continue** calls the opt-in and connect routes of the new service; after an own account moves to another login, the dialog opens once the listing shows the new login, not on the row before the save.
- Row: the link shows only on a row on the tools' account with a connected account, and not in its menu; the labels above appear.
- Live, locally against the real API with demo state: screenshots of each walkthrough step, taken before review.
