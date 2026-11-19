#!/bin/sh
# Set up Apple signing for the Mac installer, so it opens without a security warning.
# Needs an Apple Developer Program membership. Two steps:
#
#   sh scripts/setup_mac_signing.sh request
#       Makes a private key and a certificate request (CertificateSigningRequest.certSigningRequest)
#       in ~/.choir-solfa-signing. Upload the request at developer.apple.com > Certificates > +
#       > Developer ID Application, then download the certificate it gives you.
#
#   sh scripts/setup_mac_signing.sh secrets
#       Uses the downloaded certificate (developerID_application.cer) and an App Store Connect
#       API key (AuthKey_XXXXXXXXXX.p8) from ~/Downloads, saves them as GitHub secrets for this
#       repository, and starts the "mac installer" workflow to publish a signed installer.
#
# The private key never leaves this Mac except inside the encrypted GitHub secret.
set -eu

DIR="$HOME/.choir-solfa-signing"
OPENSSL=/usr/bin/openssl   # macOS's own; its .p12 files import cleanly into the keychain
REPO=d-beloved/choir-solfa

say() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
fail() { printf '\n\033[1;31m%s\033[0m\n' "$1" >&2; exit 1; }

request() {
  mkdir -p "$DIR" && chmod 700 "$DIR"
  if [ -f "$DIR/key.pem" ]; then
    say "A key already exists in $DIR; reusing it"
  else
    "$OPENSSL" genrsa -out "$DIR/key.pem" 2048 2>/dev/null
    chmod 600 "$DIR/key.pem"
  fi
  "$OPENSSL" req -new -key "$DIR/key.pem" -out "$DIR/CertificateSigningRequest.certSigningRequest" \
    -subj "/CN=Choir Solfa Developer ID/O=Choir Solfa"
  say "Certificate request ready"
  echo "File: $DIR/CertificateSigningRequest.certSigningRequest"
  echo "Upload it at https://developer.apple.com/account/resources/certificates/add"
  echo "(choose Developer ID Application, then G2 Sub-CA), and download the certificate."
  open "$DIR" 2>/dev/null || true
}

newest() { ls -t $1 2>/dev/null | head -1; }

secrets() {
  [ -f "$DIR/key.pem" ] || fail "No key yet. Run: sh scripts/setup_mac_signing.sh request"
  command -v gh >/dev/null || fail "This needs the GitHub command line tool (gh)."

  CER="${1:-$(newest "$HOME/Downloads/developerID_application*.cer")}"
  P8="${2:-$(newest "$HOME/Downloads/AuthKey_*.p8")}"
  [ -n "$CER" ] && [ -f "$CER" ] || fail "Can't find developerID_application.cer in Downloads."
  [ -n "$P8" ] && [ -f "$P8" ] || fail "Can't find an AuthKey_....p8 file in Downloads."
  KEY_ID="$(basename "$P8" .p8 | sed 's/^AuthKey_//')"
  echo "Certificate: $CER"
  echo "API key:     $P8 (Key ID $KEY_ID)"

  "$OPENSSL" x509 -inform DER -in "$CER" -out "$DIR/cert.pem"
  SUBJECT="$("$OPENSSL" x509 -in "$DIR/cert.pem" -noout -subject)"
  case "$SUBJECT" in *"Developer ID Application"*) ;; *) fail "That certificate isn't a Developer ID Application one: $SUBJECT" ;; esac
  [ "$("$OPENSSL" x509 -in "$DIR/cert.pem" -noout -modulus)" = "$("$OPENSSL" rsa -in "$DIR/key.pem" -noout -modulus 2>/dev/null)" ] \
    || fail "That certificate was made from a different request. Upload $DIR/CertificateSigningRequest.certSigningRequest and download again."

  curl -fsSL https://www.apple.com/certificateauthority/DeveloperIDG2CA.cer -o "$DIR/DeveloperIDG2CA.cer"
  "$OPENSSL" x509 -inform DER -in "$DIR/DeveloperIDG2CA.cer" -out "$DIR/DeveloperIDG2CA.pem"
  "$OPENSSL" rand -hex 24 > "$DIR/p12-password"
  chmod 600 "$DIR/p12-password"
  "$OPENSSL" pkcs12 -export -inkey "$DIR/key.pem" -in "$DIR/cert.pem" -certfile "$DIR/DeveloperIDG2CA.pem" \
    -name "Choir Solfa Developer ID" -out "$DIR/cert.p12" -passout "file:$DIR/p12-password"
  chmod 600 "$DIR/cert.p12"

  printf '\nIssuer ID (shown above the keys list in App Store Connect > Users and Access > Integrations): '
  read -r ISSUER
  [ -n "$ISSUER" ] || fail "The Issuer ID is needed."

  say "Saving the GitHub secrets for $REPO"
  base64 -i "$DIR/cert.p12" | gh secret set MACOS_CERT_P12 -R "$REPO"
  gh secret set MACOS_CERT_PASSWORD -R "$REPO" < "$DIR/p12-password"
  gh secret set NOTARY_KEY_P8 -R "$REPO" < "$P8"
  printf '%s' "$KEY_ID" | gh secret set NOTARY_KEY_ID -R "$REPO"
  printf '%s' "$ISSUER" | gh secret set NOTARY_ISSUER_ID -R "$REPO"

  say "Publishing a signed installer"
  gh workflow run mac-installer.yml -R "$REPO"
  echo "Started. It takes a few minutes (Apple checks the app). Follow it with:"
  echo "  gh run watch -R $REPO \$(gh run list -R $REPO -w mac-installer.yml -L 1 --json databaseId -q '.[0].databaseId')"
  echo
  echo "Keep $DIR somewhere safe (it holds your signing key). You can delete the .p8 file from Downloads."
}

case "${1:-}" in
  request) request ;;
  secrets) shift; secrets "$@" ;;
  *) echo "Use: sh scripts/setup_mac_signing.sh request | secrets [certificate.cer] [AuthKey_ID.p8]"; exit 1 ;;
esac
