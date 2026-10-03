## Skill: table extraction

Fill array fields, such as `line_items`, from tables.

- Use the table whose header matches the item fields best. Map columns by meaning.
- Return one entry per billed row, in document order. Skip header rows, blank rows,
  and subtotal, tax and total rows.
- Cite the first cell of each row as that entry's source.
- If no table holds the items, return an empty list.
