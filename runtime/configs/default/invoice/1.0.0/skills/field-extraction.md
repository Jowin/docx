## Skill: field extraction

Find each scalar field in the data dictionary.

- Match labels by meaning, using the field's description and aliases. "Amount due",
  "Balance due" and "Total due" are the same field; "Subtotal" is not the total.
- A label and its value can sit in the same cell ("Invoice No: INV-20194"), in the
  next cell to the right, or below a column header.
- For a date written as 03/04/2026, decide the order from other dates in the same
  document; if nothing settles it, return the value as written and lower the confidence.
- For `currency`, return the ISO code. A "$" alone is USD unless the document names
  another dollar currency.
- Never compute a value that is not written, except where a field's description
  says it may be inferred.

### Records

- The data dictionary's `record_key` (here `invoice_number`) tells records apart.
  Two documents holding the same key describe the same record: merge their fields.
- A table with a key column is one record per key value; rows sharing a key are one
  record, and their item columns are its line items.
- A label outside such a table ("Supplier: Acme") applies to every record in that
  document unless the table has its own column for it.
- Content that belongs to no identifiable record (a covering note's total when there
  are several invoices) goes into no record.
