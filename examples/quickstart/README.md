# Quickstart: parity of a square

[Examples](../README.md)

Input: [EVEN_SQUARE.md](EVEN_SQUARE.md).

Complete [installation and environment checks](../../docs/installation.md#4-verify-the-installation-without-inference) first, including `pace doctor`. This proof-mode example needs no TeX or Poppler. Run the following command from the `main` repository root with the PACE environment active.

```bash
pace run --problem examples/quickstart/EVEN_SQUARE.md --problem-id even-square \
  --output outputs --mode proof \
  --model gpt-6.1-sol --reasoning-effort high \
  --web-search-mode on
```

This command calls the configured model. Use a model/effort pair available to your account. It runs one initial Survey followed by direct proving and independent verification, without Director or SMI. Writing is explicitly requested after research or proof. Read `outputs/even-square/work/turn001/final_report.json`, then the linked verification records. The Web application supplies reader briefs and downloads. A small example is not a guarantee of a particular model's success or cost.

For continuation, recovery, and batch execution of the same input, see the [user guide](../../docs/user-guide.md).
