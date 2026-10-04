## Skill: settlement fields

Find each field of a settlement instruction.

- **Settlement date vs trade date.** "Value date", "Settle date" and "SD" are the
  settlement date; "Trade date", "Deal date" and "TD" are the trade date. The
  settlement date is on or after the trade date; if they look swapped, keep them as
  labelled and lower the confidence.
- **Amount.** Prefer the net settlement amount over gross, principal or quantity.
  Brackets, a trailing "DR" or a "pay"/"out" direction mean a negative amount when the
  source signs amounts.
- **Currency.** Return the ISO code. Map symbols only when unambiguous ("€" EUR,
  "£" GBP, "₹" INR); "$" alone is USD unless the document names another dollar currency.
- **Portfolio.** Portfolio, fund or account code exactly as written.
- **Cash purpose code.** A short code such as SECU, INTC, DIVD, FEES, TAXS, CASH,
  or the client's own codes; copy it, do not translate it.
- **Transaction type.** BUY, SELL, DVP, RVP, FX, CASH IN, CASH OUT, DIVIDEND, COUPON,
  FEE and similar; copy the source's wording.
- **Security id.** ISIN (2 letters + 9 characters + check digit), CUSIP (9),
  SEDOL (7) or an internal id. Pure cash movements have none: return null.
- **Comments.** Free text tied to the instruction (a "Comments" or "Remarks"
  column, or a note for that line). Not the email signature, not disclaimers.
