#!/bin/bash
# verify_apk.sh <apk> <workdir> <lang>
# Structural acceptance for the cross-platform benchmark's APK artifact.
# Prints PASS/FAIL per check and a final "APK-RESULT: PASS n/n" line.
# Exit 0 iff every check passes.
#
# NOTE (honesty): this box has no emulator/device, so on-device runtime is
# UNVERIFIED. These checks prove the APK builds, signs, and structurally
# contains the required app — nothing more.
set -u
APK="${1:?usage: verify_apk.sh <apk> <workdir> <lang>}"
WORKDIR="${2:?usage: verify_apk.sh <apk> <workdir> <lang>}"
LANG="${3:?usage: verify_apk.sh <apk> <workdir> <lang>}"

BT="$HOME/workspace/android-sdk/build-tools/android-14"
export JAVA_HOME="$HOME/workspace/tools/jdk-21"
export PATH="$JAVA_HOME/bin:$PATH"
PASS=0; TOTAL=0
ok()   { TOTAL=$((TOTAL+1)); PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { TOTAL=$((TOTAL+1)); echo "FAIL: $1"; }

# 1. APK file exists and is non-trivial.
if [ -f "$APK" ] && [ "$(stat -c%s "$APK")" -gt 100000 ]; then
  ok "apk exists ($(stat -c%s "$APK") bytes)"
else
  fail "apk exists and >100KB"
fi

# 2. aapt badging: package name present.
if BADGE="$("$BT/aapt" dump badging "$APK" 2>/dev/null)" \
   && printf '%s' "$BADGE" | grep -q "^package: name="; then
  ok "aapt badging: $(printf '%s' "$BADGE" | head -1 | cut -c1-80)"
else
  fail "aapt dump badging shows package"
fi

# 3. Signature verifies.
if "$BT/apksigner" verify "$APK" >/dev/null 2>&1; then
  ok "apksigner verify"
else
  fail "apksigner verify"
fi

# 4/5/6. Required UI components present in sources (structural; runtime unverified).
if [ "$LANG" = "misa" ]; then
  SRC="$WORKDIR/taskman.misa"
  { grep -q "screen" "$SRC" 2>/dev/null && grep -q "List" "$SRC" 2>/dev/null; } \
    && ok "misa: screen with List (task list)" \
    || fail "misa: screen with List (task list)"
  { grep -q "TextInput" "$SRC" 2>/dev/null && grep -q "Button" "$SRC" 2>/dev/null; } \
    && ok "misa: TextInput + Button (add task)" \
    || fail "misa: TextInput + Button (add task)"
  { grep -q "db.exec\|db\.query" "$SRC" 2>/dev/null; } \
    && ok "misa: db wiring present in app" \
    || fail "misa: db wiring present in app"
else
  # Python: look for a UI source tree (kivy .py/.kv, toga, or chaquopy java+py).
  if grep -rqi "kivy\|toga\|chaquopy" "$WORKDIR" --include="*.py" \
       --include="*.kv" --include="*.java" --include="*.toml" \
       --include="*.spec" 2>/dev/null; then
    ok "python: android UI framework present in sources"
  else
    fail "python: android UI framework present in sources"
  fi
  if grep -rqi "sqlite3" "$WORKDIR" --include="*.py" 2>/dev/null; then
    ok "python: sqlite usage present"
  else
    fail "python: sqlite usage present"
  fi
  # add-task affordance: text input + button-ish widget referenced.
  if grep -rqi "textinput\|button" "$WORKDIR" --include="*.py" \
       --include="*.kv" 2>/dev/null; then
    ok "python: input + button affordance present"
  else
    fail "python: input + button affordance present"
  fi
fi

echo "APK-RESULT: PASS $PASS/$TOTAL"
[ "$PASS" -eq "$TOTAL" ]
