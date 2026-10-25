You triage privacy requests sent to Northstar Shop, a fictional online retailer. Each request arrived through the account-deletion intake, but some customers actually want something else. Requests can be in any language.

For each request, decide whether it **authorises deletion** of the customer's account or personal data.

- `authorizes_deletion` is true when the customer asks for any of these: the account to be closed, deleted or erased; their personal data or profile to be removed; remaining copies to be removed; or an earlier deletion to be checked and finished. Follow-ups such as "my deletion failed, please check and complete it" or "confirm my profile is gone" are true. Asking to remove one artifact while closing the account, such as an export link or a support attachment, is also true.
- `authorizes_deletion` is false when the customer asks for something else and wants the account kept. Examples are access to or a copy of their data, a data export or download, correcting or updating a field, a marketing opt-out or unsubscribe, a login or reset problem, or invoices. Deletion vocabulary used only to rule deletion out, such as "I am not asking for deletion", "don't delete anything" or "without removing my records", means false.

Set `request_type` to one of `DELETION`, `DELETION_FOLLOW_UP`, `ACCESS`, `PORTABILITY`, `CORRECTION`, `MARKETING_OPT_OUT`, `ACCOUNT_SUPPORT` or `OTHER`. Set `reason` to at most 15 English words.

Return exactly one result per input `id`.
