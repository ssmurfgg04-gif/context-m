# VOUCHED.td — maintainer-curated trust list for the PR gate.
#
# Format: one handle per line. Prefix with `-` to DENOUNCE a handle
# (their PRs will NOT auto-reopen even if an `accepted` issue is linked).
# Lines starting with `#` are comments and ignored.
#
# Optional platform prefix: `github:octocat`, `gitlab:foo`, etc.
# If no prefix, `github:` is assumed.
#
# Sort alphabetically. Empty file = nobody vouched, nobody denounced,
# gate operates purely on the `accepted` label. That's the safe default
# for a young repo.
#
# Example entries (uncomment to use):
# github:mitchellh
# github:kamranahmedse
# -github:spammer-handle
