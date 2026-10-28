# Choosing the Account of an Agent Keys Service

This record explains how a user gives an agent a second account of a login provider (for example a second Gmail account) on the Agent Keys page, without finding a hidden checkbox or being offered an API key the service cannot use.

## Why

A service of the user's own can use its own account since the separate-accounts change, but the page hides that.
A user who adds a Gmail service sees a Preset, a Name and an **API key** field, which Gmail never uses, and up to six orange paragraphs about routing order.
The account choice is a checkbox under **More options**, unticked, between path rules and placeholder rules, next to a line written for administrators ("Users can connect an account (Gmail) instead of an API key.").
Saving without it gives a second row that is identical to the administrator's: the same account, the same **Use account** button.
The quickest way in, **Add another account**, sits in the row's **⋯** menu.

Five of the six warnings are also wrong: the check compares hosts only, so Drive (`www.googleapis.com/drive/`) is reported as colliding with Gmail (`www.googleapis.com/gmail/`).

## Walkthrough

For a user whose Mind already has Gmail from their administrator, signed in as their own address.

From the row:

1. On the Agent Keys page, the Gmail row shows the user's address and a link, **Add another Gmail account**.
2. They click it. A new Gmail row appears under **Added by you**, and a box asks what agents may do with the new account: **Read only** (picked) or **Read and write**.
3. They click **Continue**. Google's sign-in opens and they pick the other account.
4. The new row shows that account's address and "Using connected account, Read only". Agents use it by naming it in a request header, which the row shows in small print.

From **Add service**:

1. They click **Add service** and pick Gmail. The form asks **Account**: "The account your tools use (their address)" or "Another Gmail account". The second is already picked, because the Gmail row uses the first; a line under the first says so.
2. They click **Save and sign in**. Steps 2 to 4 above follow.

## Decisions

- **The form asks which account, in place of the API key.**
  Where the form today offers the separate-account checkbox (`offersSeparateAccount`: a personal service whose login is not requester-scoped, on an agent that is not shared), it shows a field **Account** with two choices instead:
  "The account your tools use", followed by that account's address when another service of the agent shows it, saves `oauth_separate_account: false`;
  "Another {provider} account", with the hint "You sign in right after saving. Agents pick it with `X-Agent-Keys-Service: {name}`.", saves `true`.
  For a preset the field sits in the main form, right after Preset; for a custom service it stays in **More options**, right after the login select it depends on. The checkbox goes away.
  A new service starts on "Another" when another service of the agent (not a limit) already uses the same provider without its own account, and on "The account your tools use" otherwise; choosing the tools' account while such a service exists shows "{that service} already uses this account." under it.
  Editing shows the saved choice; switching keeps today's cleanup on save.
- **No API key in that form.** The API key field and the line "Users can connect an account (…) instead of an API key." are not shown while the Account field is. A service on the tools' account can still take a key from its row (**Use an API key instead**). Administrators' forms and shared agents keep today's fields.
- **Saving a new own account signs in next.** When the saved service will have its own account and did not have one before, the save button reads **Save and sign in**. After the save succeeds, the editor closes and the saved row opens its existing allow dialog (level choice where read only is offered, starting on Read only); **Continue** opens the provider's sign-in. Cancelling leaves the row unconnected with its **Connect** button. Any other save keeps **Save service**.
- **The row's way in is a link.** A row that offers **Add another account** today and has no account of its own shows it under its status as a link, **Add another {provider} account**, and no longer in its menu. A click saves the new service at once, through the same route the editor uses: the provider's preset, `oauth_separate_account: true`, the editor's next free default name (`{preset}-2`, …), no display name. The new row then opens its allow dialog as above. A failed save shows the existing error and creates nothing.
- **Warnings are about real overlaps, said once.** A service warns about another only where a rule of each sends a credential to the same host and port (a missing port matches any) and one path prefix covers the other, by segment (an empty prefix covers the whole host). The warning reads "{other} already handles {addresses} for this agent, so requests there may use it instead of this service.", listing each shared address once. A service with its own account shows no overlap warning and no routing note: its requests name it.
- **Placeholder rules only when placeholders exist.** The paragraph of placeholder naming rules shows only while the list has an entry; the one-paragraph explanation and **Add placeholder** stay.
- **The row says what agents can do.** The status "Connected for tools only" becomes "Not used by agents yet"; the button and the allow dialog's confirm "Use account" become "Let agents use it"; the note under such a row becomes "Agents' requests are refused until you let them use this account."

## Later, not now

- Naming an own-account row after its address once signed in.
- Removing an own-account service whose sign-in was cancelled.
- Moving the Name field out of the main form.

## Assumptions

- No backend or API change: the PUT service route, the opt-in route and the connect flow already do everything above.
- The provider's display name reads naturally in "Add another {provider} account" (Gmail, Google Calendar, Google Drive).

## Testing

- Overlap: Gmail against Drive, Calendar and Tasks presets warns nothing; Gmail against Gmail warns once with both addresses; a whole-host rule overlaps every path on its host; different ports do not overlap.
- Editor: a Gmail preset shows Account and no API key field; the default follows the rule above; each choice saves the matching `oauth_separate_account`; the button reads **Save and sign in** only for a new own account; the custom-service path keeps the choice in More options; administrator and shared-agent forms are unchanged.
- After **Save and sign in** and after the row link, the new row's allow dialog is open, and **Continue** calls the opt-in and connect routes of the new service.
- Row: the link replaces the menu item, only on rows without their own account; the new texts appear.
- Live, locally against the real API with demo state: screenshots of each walkthrough step, taken before review.
