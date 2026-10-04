## Skill: settlement rows

Settlement emails usually carry a blotter: a table with one instruction per row.

- Use the table whose header matches the most settlement fields. Each data row is
  one record, in document order.
- Skip header rows, blank rows and subtotal/total rows.
- Columns the table lacks are filled from labels elsewhere in the same document
  ("Portfolio: GLB-EQ-01" above the table) when they clearly apply to all rows.
- An email with no table but labelled values ("Settlement Date: 06-Oct-2026",
  "Amount: 1,250,000.00 USD") is a single instruction.
