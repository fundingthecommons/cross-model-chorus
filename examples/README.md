# Examples

Two synthetic files for trying the chorus without sending anything real. Every organization, person and figure in them is invented.

- `grant-application.md`: a grant application with problems planted on purpose.
- `funder-call.md`: the (invented) funder's published call, to pass as `--criteria`.

Try the grant use case on them. A dry run sends nothing and checks no key with any provider, but it plans only the voices whose key variable is set (with none set, it exits 3). Any placeholder value will do:

```bash
OPENAI_API_KEY=x PHALA_API_KEY=x python3 chorus.py --use-case grant --sensitivity internal --file examples/grant-application.md --criteria examples/funder-call.md --dry-run
```

For a real review, set your real keys and run the same command with `--yes` in place of `--dry-run`.

## What is planted

Read the report against this list to see what the chorus caught and what it missed. Don't show the list to the reviewers.

1. **An impossible growth claim.** 3 trusts to 4,000 in two years, while the pilot that ended in 2026 ran with three trusts.
2. **A date contradiction.** The pilot "finished in June 2026, two months after it began in August 2026".
3. **Budget arithmetic.** The lines add up to USD 88,000, not the USD 85,000 total.
4. **Overhead over the cap.** The call caps overhead at 10%; the draft says 15%, and its own figure (12,000 of 85,000) is about 14%.
5. **An output presented as an outcome.** "1,200 volunteers trained" counts activity, not change, and the call asks for measured outcomes.
6. **A match from an unnamed source, and only pledged.** The call asks applicants to name any matching funds and say whether they are secured or pledged. The draft says "pledged" but names only "a regional foundation", and a pledged match is weaker than a secured one. Flagging either gap counts as a catch.
7. **An unsourced statistic.** "Our survey of trusts found that most had lost at least one important record" gives no sample, method or date.
8. **A thin maintenance plan.** The draft hands maintenance to "a steering group of five trusts" but names no developer, budget or funding for it after the grant ends. The only developer is paid from the 12-month grant, and "the steering group will cover hosting" covers hosting, not upkeep of the code. The call asks how the software will be maintained after the grant.
