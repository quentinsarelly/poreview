<!--
Record of the outstanding request to Spring Systems. Sent 2026-08-28; a
follow-up was also sent asking what invoice_status values mean and where a
997 functional acknowledgment from Target is visible.

STATUS 2026-09-01: Spring granted the permission (reply from Ahmad). Verified
with a read-only OPTIONS probe -- 405 "no permission" became 400 "unable to
read the data you sent", i.e. authorization now passes.

STILL OPEN: the draft-vs-810 question below was not answered. Until it is,
SPRING_INVOICE_ENABLED stays off, /po-invoice creates only the Odoo draft, and
the Spring invoice is raised manually in the portal. See README "Open
questions" for how to settle it on the next PO that needs invoicing.

Delete this file once that question is answered and the flag is on.
-->

Subject: API permission needed for invoice-incoming/send + question on draft vs. send behavior

Hi,

We're working on automating part of our invoicing process -- once a PO has shipped, we want to submit the invoice for it via the API instead of doing it manually in the portal.

We're hitting a permissions error when calling `POST /invoice-incoming/send`:

    Status: 405 Method Not Allowed
    Body: {"errors":["You do not have permission to use this resource"]}

This is with our API user `sarelly_odoo_prod_api` (retailer_id 135, vendor tp_id 33145). Could you enable the "send invoice" permission for this API user?

Separately, before we rely on this endpoint: can you confirm whether a call to `invoice-incoming/send` creates a draft/pending invoice on our side, or whether it immediately transmits an EDI 810 invoice to the retailer? We want to make sure we understand the effect of this call before using it in an automated flow.

Thanks,
