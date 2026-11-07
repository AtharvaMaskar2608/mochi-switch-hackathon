#!/usr/bin/env bash
# Builds the "brightcart-shop" demo repo.
#   --local <dir>      build history locally (no network, no remote)
#   --publish <owner> [--private]   create GitHub repo, push, open+merge 3 PRs (asks first)
# Planted: BC-142 rounding bug in refunds.py (xfail test), requests==2.19.1 (known CVEs).
# --local only: unmerged branch fix/refund-rounding, a wrong BC-142 fix whose test fails (QA FAILED demo).
set -euo pipefail

export GIT_AUTHOR_NAME="Asha Rao" GIT_AUTHOR_EMAIL="asha@brightcart.example"
export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME" GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"

at() { # at <days-ago>: backdate the next commit/merge/tag
  local t; t=$(date -u -v-"$1"d +%Y-%m-%dT12:00:00Z 2>/dev/null || date -u -d "$1 days ago" +%Y-%m-%dT12:00:00Z)
  export GIT_AUTHOR_DATE="$t" GIT_COMMITTER_DATE="$t"
}
commit() { git add -A && git commit -q -m "$1"; }

base() { # three commits straight on main (days 7..5)
  git init -q -b main
  mkdir tests
  cat > cart.py <<'PY'
"""Shopping cart. All money is integer paise."""


def total(items):
    return sum(i["price"] * i["qty"] for i in items)
PY
  echo "# brightcart-shop" > README.md
  : > conftest.py   # empty: puts repo root on sys.path so tests can import cart/refunds
  at 7; commit "Initial cart"

  cat > refunds.py <<'PY'
"""Refunds. All money is integer paise."""


def full_refund(paid):
    return paid
PY
  cat > tests/test_refunds.py <<'PY'
from refunds import full_refund


def test_full_refund():
    assert full_refund(1999) == 1999
PY
  echo "requests==2.19.1" > requirements.txt   # planted: known-vulnerable dependency
  at 6; commit "Add full refunds and pin requests"

  cat > tests/test_cart.py <<'PY'
from cart import total


def test_total():
    assert total([{"price": 500, "qty": 2}]) == 1000
PY
  at 5; commit "Add cart tests"
}

pr1() { git checkout -q -b feat/coupon-codes
  cat >> cart.py <<'PY'


def apply_coupon(amount, percent):
    return amount - amount * percent // 100
PY
  at 4; commit "Add percentage coupons"; }

pr2() { git checkout -q -b feat/partial-refunds
  cat >> refunds.py <<'PY'


def partial_refund(paid, percent):
    # BC-142: float math + round() is banker's rounding, off by a paisa on half-values
    return round(paid * percent / 100)
PY
  cat >> tests/test_refunds.py <<'PY'

import pytest
from refunds import partial_refund


@pytest.mark.xfail(reason="known bug BC-142")
def test_partial_refund_half_paisa_rounds_up():
    assert partial_refund(25, 50) == 13  # 12.5 paise -> 13, round() gives 12
PY
  at 3; commit "Add partial refunds"; }

pr3() { git checkout -q -b docs/readme-usage
  printf '\nRun tests with `pytest`. Money is integer paise.\n' >> README.md
  at 2; commit "Document usage in README"; }

PR_TITLES=("Add percentage coupons" "Add partial refunds" "Document usage in README")
PR_BRANCHES=(feat/coupon-codes feat/partial-refunds docs/readme-usage)
PR_FNS=(pr1 pr2 pr3)

local_build() {
  mkdir -p "$1" && cd "$1"
  base
  for i in 0 1 2; do
    git checkout -q main; ${PR_FNS[$i]}; git checkout -q main
    at $((3 - i)); git merge -q --no-ff "${PR_BRANCHES[$i]}" -m "Merge PR #$((i + 1)): ${PR_TITLES[$i]}"
  done
  at 1; git tag -a v1.4.1 -m "v1.4.1"
  qa_fail_branch; git checkout -q main
}

qa_fail_branch() { # unmerged, for the QA demo: a wrong BC-142 fix whose test now really fails
  git checkout -q -b fix/refund-rounding main
  sed -i.bak 's|    return round(paid \* percent / 100)|    return paid * percent // 100  # floors: 12.5 -> 12, still wrong|' refunds.py
  sed -i.bak '/@pytest.mark.xfail(reason="known bug BC-142")/d' tests/test_refunds.py
  rm -f refunds.py.bak tests/test_refunds.py.bak
  at 0; commit "BC-142: fix partial refund rounding"; }

publish() {
  local owner="$1" vis="${2:---public}" repo="$1/brightcart-shop"
  echo "This will CREATE ${vis#--} GitHub repo $repo, push, and open+merge 3 PRs."
  read -r -p "Type the repo name (brightcart-shop) to continue: " ans
  [ "$ans" = brightcart-shop ] || { echo "aborted"; exit 1; }
  local dir; dir=$(mktemp -d); cd "$dir"
  base
  for i in 0 1 2; do git checkout -q main; ${PR_FNS[$i]}; done; git checkout -q main
  gh repo create "$repo" "$vis" --source . --push
  for i in 0 1 2; do
    git push -q origin "${PR_BRANCHES[$i]}"
    gh pr create -R "$repo" --base main --head "${PR_BRANCHES[$i]}" --title "${PR_TITLES[$i]}" --body "${PR_TITLES[$i]}."
    gh pr merge -R "$repo" "${PR_BRANCHES[$i]}" --merge
  done
  git pull -q origin main --no-rebase
  git tag -a v1.4.1 -m "v1.4.1" && git push -q origin v1.4.1
  gh release create v1.4.1 -R "$repo" --title v1.4.1 --notes "Coupons, partial refunds, docs."
}

case "${1:-}" in
  --local) [ -n "${2:-}" ] || { echo "usage: $0 --local <dir>"; exit 2; }; local_build "$2" ;;
  --publish) [ -n "${2:-}" ] || { echo "usage: $0 --publish <owner> [--private]"; exit 2; }; publish "$2" "${3:-}" ;;
  *) echo "usage: $0 --local <dir> | --publish <owner> [--private]"; exit 2 ;;
esac
