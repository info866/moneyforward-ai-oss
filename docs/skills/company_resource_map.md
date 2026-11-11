# Company resource map (private runtime bindings)

This tracked file is **an instruction template only**. It contains no
company name, real email, office number, Drive ID or credential.
Use this company's `config/offices/<slug>.json` **ignored local profile**
plus the authenticated company-specific connector and DB's canonical
resource mapping to resolve all fields:

| Binding | Authoritative verification |
| --- | --- |
| Legal entity / accounting office | Official Money Forward office read-back |
| Accounting principal | Authenticated provider session and exact profile |
| Keychain/secret store namespace | The company's configured, exclusive key |
| DB/RAG | The company's existing, bound canonical DB |
| Google Workspace / Gmail | Authenticated principal (not email string alone) |
| Google Drive | Authenticated actor and exact source/destination folder IDs |
| Payroll timecards and wage ledger | This company's verified employee/period source |
| Final Google Chat | This company's approved webhook and durable ledger |
| Existing run/write MCP | Exact target company; verify actual tools + scopes |

Never put raw IDs, employee records, OAuth tokens, evidence, payments or
company accounting rules into this tracked file, Issues or plugin examples.
If any required resource is missing, mark the affected workflow pending;
never fill it by borrowing another company or creating an extra account.
