#!/usr/bin/env bash
# Runs on macOS. Secrets come from the environment, never command-line arguments.
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="$ROOT_DIR/frontend"
OUTPUT_DIR="${NABD_IOS_OUTPUT_DIR:-$ROOT_DIR/build/ios}"
SIGN_IPA=false
EXPORT_METHOD=ad-hoc

fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --signed) SIGN_IPA=true; shift ;;
    --unsigned) SIGN_IPA=false; shift ;;
    --export-method)
      [ "$#" -ge 2 ] || fail '--export-method requires a value'
      EXPORT_METHOD="$2"; shift 2 ;;
    --help|-h)
      printf '%s\n' 'Usage: bash scripts/build-ios.sh [--unsigned | --signed] [--export-method ad-hoc|development|app-store]'
      exit 0 ;;
    *) fail "Unknown argument: $1" ;;
  esac
done

case "$EXPORT_METHOD" in
  ad-hoc) XCODE_EXPORT_METHOD=release-testing ;;
  development) XCODE_EXPORT_METHOD=debugging ;;
  app-store) XCODE_EXPORT_METHOD=app-store-connect ;;
  *) fail 'Export method must be ad-hoc, development, or app-store' ;;
esac

[ "$(uname -s)" = Darwin ] || fail 'A real iOS device build requires macOS/Xcode. Use the GitHub Actions workflow.'
for executable in npm npx xcodebuild python3; do
  command -v "$executable" >/dev/null || fail "Required executable is missing: $executable"
done

if [ "$SIGN_IPA" = true ]; then
  for secret_name in IOS_P12_BASE64 IOS_P12_PASSWORD IOS_PROFILE_BASE64 IOS_TEAM_ID; do
    [ -n "${!secret_name:-}" ] || fail "Set the $secret_name environment variable or GitHub Actions repository secret"
  done
  command -v ruby >/dev/null || fail 'Ruby is required for signing'
  ruby -rxcodeproj -e 'exit 0' || fail 'Install the xcodeproj Ruby gem: gem install xcodeproj -v 1.27.0'
fi

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"
# Refuse to mix an old output with the current signing mode.
if [ -e "$OUTPUT_DIR/Nabd.xcarchive" ] || [ -e "$OUTPUT_DIR/Payload" ]; then
  fail 'Output contains an earlier archive. Choose a fresh NABD_IOS_OUTPUT_DIR or remove the earlier build/ios output.'
fi

TEMP_DIR="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/nabd-ios.XXXXXX")"
KEYCHAIN_PATH=''
PROFILE_PATH=''
PROFILE_BACKUP=''
PROJECT_BACKUP=''
ORIGINAL_KEYCHAINS=()

cleanup() {
  local result=$?
  trap - EXIT
  if [ -n "$PROJECT_BACKUP" ] && [ -f "$PROJECT_BACKUP" ]; then
    cp "$PROJECT_BACKUP" "$FRONTEND_DIR/ios/App/App.xcodeproj/project.pbxproj" || true
  fi
  if [ -n "$PROFILE_PATH" ]; then
    if [ -n "$PROFILE_BACKUP" ] && [ -f "$PROFILE_BACKUP" ]; then
      cp "$PROFILE_BACKUP" "$PROFILE_PATH" || true
    else
      rm -f "$PROFILE_PATH" || true
    fi
  fi
  if [ -n "$KEYCHAIN_PATH" ]; then
    if [ "${#ORIGINAL_KEYCHAINS[@]}" -gt 0 ]; then
      security list-keychains -d user -s "${ORIGINAL_KEYCHAINS[@]}" >/dev/null 2>&1 || true
    fi
    security delete-keychain "$KEYCHAIN_PATH" >/dev/null 2>&1 || true
  fi
  rm -rf "$TEMP_DIR"
  exit "$result"
}
trap cleanup EXIT

cd "$FRONTEND_DIR"
npm run build
if [ ! -f ios/App/App.xcodeproj/project.pbxproj ]; then npx cap add ios --packagemanager SPM; fi
if [ -f ios/App/Podfile ]; then
  command -v pod >/dev/null || fail 'This native project uses CocoaPods. Install CocoaPods before syncing.'
fi
npx cap sync ios

WORKSPACE="$FRONTEND_DIR/ios/App/App.xcworkspace"
PROJECT="$FRONTEND_DIR/ios/App/App.xcodeproj"
if [ -d "$WORKSPACE" ]; then
  BUILD_CONTAINER=(-workspace "$WORKSPACE")
elif [ -d "$PROJECT" ]; then
  BUILD_CONTAINER=(-project "$PROJECT")
else
  fail 'Capacitor did not generate an iOS Xcode project'
fi

if [ "$SIGN_IPA" = true ]; then
  export NABD_SIGNING_TEMP="$TEMP_DIR" NABD_EXPORT_METHOD="$EXPORT_METHOD"
  python3 <<'PY'
import base64
import os
from pathlib import Path

directory = Path(os.environ['NABD_SIGNING_TEMP'])
for source, filename in [('IOS_P12_BASE64', 'certificate.p12'), ('IOS_PROFILE_BASE64', 'profile.mobileprovision')]:
    raw = ''.join(os.environ[source].split())
    try:
        value = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise SystemExit(f'{source} must contain a valid base64-encoded file') from exc
    if not value:
        raise SystemExit(f'{source} decoded to an empty file')
    (directory / filename).write_bytes(value)
PY

  security cms -D -i "$TEMP_DIR/profile.mobileprovision" > "$TEMP_DIR/profile.plist"
  python3 <<'PY'
import datetime
import fnmatch
import os
import plistlib
from pathlib import Path

directory = Path(os.environ['NABD_SIGNING_TEMP'])
with (directory / 'profile.plist').open('rb') as handle:
    profile = plistlib.load(handle)
team = os.environ['IOS_TEAM_ID']
if team not in profile.get('TeamIdentifier', []):
    raise SystemExit('IOS_TEAM_ID does not match the provisioning profile')
bundle_id = 'dev.nabd.app'
app_id = profile.get('Entitlements', {}).get('application-identifier', '')
prefixes = profile.get('ApplicationIdentifierPrefix', [team])
if not any(fnmatch.fnmatchcase(f'{prefix}.{bundle_id}', app_id) for prefix in prefixes):
    raise SystemExit('Provisioning profile does not cover dev.nabd.app')
expires = profile.get('ExpirationDate')
if not expires or expires.replace(tzinfo=datetime.timezone.utc) <= datetime.datetime.now(datetime.timezone.utc):
    raise SystemExit('Provisioning profile has expired')
development = bool(profile.get('Entitlements', {}).get('get-task-allow'))
devices = bool(profile.get('ProvisionedDevices'))
method = os.environ['NABD_EXPORT_METHOD']
if method == 'development' and not (development and devices):
    raise SystemExit('Choose a development provisioning profile for a development export')
if method == 'ad-hoc' and not (devices and not development):
    raise SystemExit('Choose an Ad Hoc provisioning profile with registered devices for an ad-hoc export')
if method == 'app-store' and (devices or development or profile.get('ProvisionsAllDevices')):
    raise SystemExit('Choose an App Store provisioning profile for an app-store export')
uuid = profile.get('UUID', '')
if not uuid or any(character not in '0123456789abcdefABCDEF-' for character in uuid):
    raise SystemExit('Provisioning profile UUID is invalid')
(directory / 'profile-uuid.txt').write_text(uuid)
PY

  PROFILE_UUID="$(cat "$TEMP_DIR/profile-uuid.txt")"
  PROFILE_DIRECTORY="$HOME/Library/MobileDevice/Provisioning Profiles"
  mkdir -p "$PROFILE_DIRECTORY"
  PROFILE_PATH="$PROFILE_DIRECTORY/${PROFILE_UUID}.mobileprovision"
  if [ -f "$PROFILE_PATH" ]; then
    PROFILE_BACKUP="$TEMP_DIR/original.mobileprovision"
    cp "$PROFILE_PATH" "$PROFILE_BACKUP"
  fi
  cp "$TEMP_DIR/profile.mobileprovision" "$PROFILE_PATH"

  while IFS= read -r entry; do
    entry="${entry#"${entry%%[![:space:]]*}"}"
    entry="${entry#\"}"; entry="${entry%\"}"
    [ -z "$entry" ] || ORIGINAL_KEYCHAINS+=("$entry")
  done < <(security list-keychains -d user)
  KEYCHAIN_PATH="$TEMP_DIR/build.keychain-db"
  KEYCHAIN_PASSWORD="$(openssl rand -hex 32)"
  security create-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH"
  security set-keychain-settings -lut 21600 "$KEYCHAIN_PATH"
  security unlock-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH"
  security import "$TEMP_DIR/certificate.p12" -k "$KEYCHAIN_PATH" -P "$IOS_P12_PASSWORD" -T /usr/bin/codesign -T /usr/bin/security >/dev/null
  security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH" >/dev/null
  security list-keychains -d user -s "$KEYCHAIN_PATH" "${ORIGINAL_KEYCHAINS[@]}"
  security find-identity -v -p codesigning "$KEYCHAIN_PATH" > "$TEMP_DIR/identities.txt"
  SIGNING_IDENTITY="$(python3 <<'PY'
import hashlib
import os
import plistlib
import re
from pathlib import Path
directory = Path(os.environ['NABD_SIGNING_TEMP'])
with (directory / 'profile.plist').open('rb') as handle:
    profile = plistlib.load(handle)
allowed = {hashlib.sha1(certificate).hexdigest().upper() for certificate in profile.get('DeveloperCertificates', [])}
identities = re.findall(r'^\s*\d+\)\s+([A-Fa-f0-9]{40})\s', (directory / 'identities.txt').read_text(), re.MULTILINE)
matching = [identity for identity in identities if identity.upper() in allowed]
if not matching:
    raise SystemExit('P12 must contain a valid code-signing certificate/private key listed in this provisioning profile')
print(matching[0])
PY
)"

  PROJECT_BACKUP="$TEMP_DIR/project.pbxproj"
  cp ios/App/App.xcodeproj/project.pbxproj "$PROJECT_BACKUP"
  export NABD_SIGNING_IDENTITY="$SIGNING_IDENTITY" NABD_PROFILE_UUID="$PROFILE_UUID"
  # Apply the profile only to App; global profile overrides break CocoaPods targets.
  ruby -rxcodeproj <<'RUBY'
project = Xcodeproj::Project.open('ios/App/App.xcodeproj')
target = project.targets.find { |item| item.name == 'App' }
abort 'App target was not found' unless target
target.build_configurations.each do |configuration|
  settings = configuration.build_settings
  settings['DEVELOPMENT_TEAM'] = ENV.fetch('IOS_TEAM_ID')
  settings['CODE_SIGN_STYLE'] = 'Manual'
  settings['CODE_SIGN_IDENTITY'] = ENV.fetch('NABD_SIGNING_IDENTITY')
  settings['PROVISIONING_PROFILE_SPECIFIER'] = ENV.fetch('NABD_PROFILE_UUID')
end
project.save
RUBY

  xcodebuild "${BUILD_CONTAINER[@]}" -scheme App -configuration Release \
    -destination 'generic/platform=iOS' -archivePath "$OUTPUT_DIR/Nabd.xcarchive" \
    IPHONEOS_DEPLOYMENT_TARGET=15.0 archive -quiet 2>&1 | tee "$OUTPUT_DIR/archive.log"

  export NABD_XCODE_EXPORT_METHOD="$XCODE_EXPORT_METHOD"
  python3 <<'PY'
import os
import plistlib
from pathlib import Path
options = {
    'method': os.environ['NABD_XCODE_EXPORT_METHOD'],
    'teamID': os.environ['IOS_TEAM_ID'],
    'signingStyle': 'manual',
    'signingCertificate': os.environ['NABD_SIGNING_IDENTITY'],
    'provisioningProfiles': {'dev.nabd.app': os.environ['NABD_PROFILE_UUID']},
    'stripSwiftSymbols': True,
    'manageAppVersionAndBuildNumber': False,
}
with (Path(os.environ['NABD_SIGNING_TEMP']) / 'ExportOptions.plist').open('wb') as handle:
    plistlib.dump(options, handle)
PY
  xcodebuild -exportArchive -archivePath "$OUTPUT_DIR/Nabd.xcarchive" \
    -exportPath "$TEMP_DIR/export" -exportOptionsPlist "$TEMP_DIR/ExportOptions.plist" \
    -quiet 2>&1 | tee "$OUTPUT_DIR/export.log"
  EXPORTED_IPA="$(find "$TEMP_DIR/export" -maxdepth 1 -type f -name '*.ipa' -print -quit)"
  [ -n "$EXPORTED_IPA" ] || fail 'Xcode export completed without an IPA'
  cp "$EXPORTED_IPA" "$OUTPUT_DIR/Nabd-${EXPORT_METHOD}.ipa"
  printf 'Apple-signed device IPA. Export method: %s. Ad Hoc/development require a device registered in this provisioning profile; App Store export is for App Store Connect/TestFlight distribution.\n' "$EXPORT_METHOD" > "$OUTPUT_DIR/SIGNING_STATUS.txt"
  ARTIFACT_NAME="Nabd-iOS-signed-${EXPORT_METHOD}"
else
  xcodebuild "${BUILD_CONTAINER[@]}" -scheme App -configuration Release \
    -destination 'generic/platform=iOS' -archivePath "$OUTPUT_DIR/Nabd.xcarchive" \
    IPHONEOS_DEPLOYMENT_TARGET=15.0 CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY='' \
    archive -quiet 2>&1 | tee "$OUTPUT_DIR/archive.log"
  APP_PATH="$OUTPUT_DIR/Nabd.xcarchive/Products/Applications/App.app"
  [ -d "$APP_PATH" ] || fail 'Archive did not contain the iOS device app'
  mkdir "$TEMP_DIR/Payload"
  ditto "$APP_PATH" "$TEMP_DIR/Payload/App.app"
  ditto -c -k --keepParent "$TEMP_DIR/Payload" "$OUTPUT_DIR/Nabd-unsigned-needs-signing.ipa"
  printf '%s\n' 'UNSIGNED iOS DEVICE BUILD. This IPA cannot be installed directly on an iPhone. It requires external Apple signing with a compatible certificate, provisioning profile, and entitlements. The xcarchive is a real device archive, not a simulator build.' > "$OUTPUT_DIR/SIGNING_STATUS.txt"
  ARTIFACT_NAME=Nabd-iOS-unsigned-needs-signing
fi

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  printf 'artifact_name=%s\n' "$ARTIFACT_NAME" >> "$GITHUB_OUTPUT"
fi
printf 'Build ready: %s\n' "$OUTPUT_DIR"
cat "$OUTPUT_DIR/SIGNING_STATUS.txt"
