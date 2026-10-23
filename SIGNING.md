# Signing LeanCut releases

The release workflow (`.github/workflows/release.yml`) signs only when the secrets below exist. Without them it
builds exactly as before: an ad-hoc signed Mac app and an unsigned Windows exe. Add secrets in GitHub under
**Settings › Secrets and variables › Actions › New repository secret**, or with `gh secret set NAME`.

Nothing else needs to change: push a `v*` tag and the signed builds are attached to the release.

## macOS: Developer ID + notarization

Without this, people have to right-click › Open (or allow the app in System Settings › Privacy & Security) the first
time. With it, the app opens normally.

**Cost:** Apple Developer Program, US$99 a year (as an individual or an organization; an organization needs a
D-U-N-S number). Notarization is free.

### 1. Join the Apple Developer Program

Enroll at <https://developer.apple.com/programs/enroll/>. Only the **Account Holder** can create Developer ID
certificates. Your **Team ID** (10 characters) is under <https://developer.apple.com/account> › Membership details.

### 2. Create the "Developer ID Application" certificate (on a Mac)

1. Open **Keychain Access** › menu **Keychain Access › Certificate Assistant › Request a Certificate From a
   Certificate Authority…**. Enter your email and name, choose **Saved to disk**, and save the `.certSigningRequest`.
2. Go to <https://developer.apple.com/account/resources/certificates/add>, choose **Developer ID Application**
   (not "Developer ID Installer", not "Apple Development"), pick the **G2 Sub-CA** profile, upload the request and
   download `developerID_application.cer`.
3. Double-click the `.cer` so it is added to your **login** keychain next to the private key from step 1.
4. Check it's there and copy its exact name:

   ```sh
   security find-identity -v -p codesigning
   #  1) ABC123… "Developer ID Application: Your Name (TEAMID1234)"
   ```

### 3. Export it as a .p12 and store the secrets

1. In Keychain Access › **login** › **My Certificates**, expand "Developer ID Application: …" so you see the private
   key under it, select the certificate, right-click › **Export…**, format **Personal Information Exchange (.p12)**,
   and set a strong password.
2. Store everything (from the folder holding the `.p12`):

   ```sh
   base64 -i Certificates.p12 | gh secret set MACOS_CERT_P12 -R lucerowb/leancut
   gh secret set MACOS_CERT_PASSWORD -R lucerowb/leancut      # the .p12 password
   gh secret set MACOS_SIGN_IDENTITY -R lucerowb/leancut      # Developer ID Application: Your Name (TEAMID1234)
   ```

   Then delete the `.p12` file. Keep a backup of the certificate and key somewhere safe (a password manager);
   Apple won't give you the private key again.

### 4. App-specific password for notarization

1. Sign in at <https://account.apple.com> › **Sign-In and Security** › **App-Specific Passwords** › **+**, name it
   "LeanCut notarization" and copy the `xxxx-xxxx-xxxx-xxxx` password.
2. Store it with your Apple ID and team:

   ```sh
   gh secret set APPLE_ID -R lucerowb/leancut            # the Apple ID email of the developer account
   gh secret set APPLE_TEAM_ID -R lucerowb/leancut       # TEAMID1234
   gh secret set APPLE_APP_PASSWORD -R lucerowb/leancut  # xxxx-xxxx-xxxx-xxxx
   ```

### What the workflow does with them

`build.py` signs `LeanCut.app` with `LEANCUT_CODESIGN_IDENTITY` (hardened runtime, timestamp,
`assets/entitlements.plist`), the workflow signs the `.dmg`, submits it with `xcrun notarytool submit --wait`, staples
the ticket to it and checks it with `stapler validate` and `spctl`.

### Verify a release (on any Mac)

```sh
hdiutil attach LeanCut-macOS.dmg
xcrun stapler validate LeanCut-macOS.dmg                                  # "The validate action worked!"
spctl -a -t open --context context:primary-signature -vv LeanCut-macOS.dmg  # accepted, source=Notarized Developer ID
codesign --verify --deep --strict --verbose=2 /Volumes/LeanCut/LeanCut.app  # valid on disk, satisfies its Designated Requirement
spctl -a -vv /Volumes/LeanCut/LeanCut.app                                 # accepted, source=Notarized Developer ID
codesign -dv --verbose=4 /Volumes/LeanCut/LeanCut.app 2>&1 | grep -E 'Authority|TeamIdentifier|Runtime'
```

### If notarization fails

`notarytool submit` prints a submission id. Ask Apple why:

```sh
xcrun notarytool log <submission-id> --apple-id you@example.com --team-id TEAMID1234 --password xxxx-xxxx-xxxx-xxxx
```

The usual cause with PyInstaller apps is a nested `.dylib`/`.so` that isn't signed with the hardened runtime and a
secure timestamp. The log names the file.

### Renewal

A Developer ID Application certificate lasts 5 years. Apps signed with a timestamp keep working after it expires;
new builds need a new certificate (repeat steps 2–3). If you leave the program, existing notarized builds keep working.

## Windows: Authenticode

Without signing, SmartScreen shows "Windows protected your PC" and people click **More info › Run anyway**. Signing
puts your name on that prompt and lets the exe build reputation over time. No option makes the prompt disappear on
day one: Microsoft stopped giving EV certificates instant SmartScreen reputation in 2024.

Since June 2023 every public code-signing certificate's private key must live in hardware (a USB token or a cloud
HSM), so a new OV or EV certificate **cannot be exported as a `.pfx`**. That leaves:

| Option | Cost (approx.) | Works with this workflow |
| --- | --- | --- |
| **Azure Artifact Signing** (formerly Trusted Signing) | US$9.99/month (Basic, 5,000 signatures) | Yes: built in, see below |
| OV certificate in a cloud HSM (DigiCert KeyLocker, SSL.com eSigner, Certum SimplySign…) | about US$130–500/year plus the HSM fee | Needs the vendor's signing tool in place of the "Sign LeanCut.exe" step |
| OV/EV certificate on a USB token | about US$200–600/year | No: the token must be plugged into the signing machine |
| A `.pfx` you already have (issued before June 2023, or self-signed for testing) | — | Yes: `WINDOWS_CERT_PFX` |

**Recommended: Azure Artifact Signing.** Microsoft's own CA, the cheapest option, no hardware. Eligibility (checked
October 2026): individual developers in the **US or Canada**; organizations in the US, Canada, EU, UK, Australia, New
Zealand, Japan, South Korea, Singapore, Switzerland, Norway and Israel. If you don't qualify, use an OV certificate
from a cloud-HSM vendor.

### Option A: Azure Artifact Signing (secrets `AZURE_*`)

1. Have an Azure subscription (<https://portal.azure.com>). For individual validation, the subscription's billing
   account must be of type **Individual**, with your legal name and address exactly as on your ID.
2. Subscriptions › your subscription › **Resource providers** › register **Microsoft.CodeSigning**.
3. Search **Artifact Signing Accounts** › **Create**: a new resource group, an account name (3–24 letters and digits,
   globally unique), a region, and pricing tier **Basic**. Note the region's endpoint, for example East US =
   `https://eus.codesigning.azure.net`.
4. On the account › **Access control (IAM)**, give yourself the **Artifact Signing Identity Verifier** role.
5. Account › **Identity validations** › **Individual** (or Organization) › **New identity › Public**, then finish the
   ID check (AU10TIX on your phone, then Microsoft Authenticator). Organizations can take 1–20 business days.
6. Account › **Certificate profiles** › **Create › Public Trust**, pick the validated identity and give the profile a
   name (5–100 letters and digits).
7. Create the identity CI signs with: Microsoft Entra ID › **App registrations › New registration** ("leancut-ci"),
   then **Certificates & secrets › New client secret**. Copy the secret value, the **Application (client) ID** and the
   **Directory (tenant) ID**.
8. On the Artifact Signing account › **Access control (IAM)**, give "leancut-ci" the **Artifact Signing Certificate
   Profile Signer** role.
9. Store the secrets:

   ```sh
   gh secret set AZURE_TENANT_ID -R lucerowb/leancut
   gh secret set AZURE_CLIENT_ID -R lucerowb/leancut
   gh secret set AZURE_CLIENT_SECRET -R lucerowb/leancut      # client secrets expire: renew before the date you chose
   gh secret set AZURE_SIGNING_ENDPOINT -R lucerowb/leancut   # https://eus.codesigning.azure.net/
   gh secret set AZURE_SIGNING_ACCOUNT -R lucerowb/leancut    # the account name from step 3
   gh secret set AZURE_CERT_PROFILE -R lucerowb/leancut       # the profile name from step 6
   ```

The workflow's "Sign LeanCut.exe (Azure Artifact Signing)" step (`azure/artifact-signing-action@v2`) runs when
`AZURE_SIGNING_ACCOUNT` is set and `WINDOWS_CERT_PFX` is not. Billing starts when you create the account; delete the
account to stop it. Its certificates last 3 days and are renewed automatically. The timestamp keeps signed exes valid.

### Option B: a .pfx file (secrets `WINDOWS_CERT_PFX`, `WINDOWS_CERT_PASSWORD`)

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("cert.pfx")) | gh secret set WINDOWS_CERT_PFX -R lucerowb/leancut
gh secret set WINDOWS_CERT_PASSWORD -R lucerowb/leancut
```

`signtool` then signs `LeanCut.exe` with a DigiCert timestamp before the zip is made.

### Verify a release (on Windows)

```powershell
Expand-Archive LeanCut-Windows.zip -DestinationPath check
Get-AuthenticodeSignature check\LeanCut\LeanCut.exe | Format-List Status, SignerCertificate, TimeStamperCertificate
# Status : Valid
& "C:\Program Files (x86)\Windows Kits\10\bin\10.0.22621.0\x64\signtool.exe" verify /pa /v check\LeanCut\LeanCut.exe
```

Only `LeanCut.exe` is signed; the bundled DLLs keep their own vendors' signatures (or none). That's enough for
SmartScreen, which checks the exe the user starts.

## Linux

No signing is needed: there's no Gatekeeper or SmartScreen. Every tagged release also gets `SHA256SUMS.txt` with the
SHA-256 of all three downloads. To check a download:

```sh
sha256sum -c --ignore-missing SHA256SUMS.txt   # Linux
shasum -a 256 -c --ignore-missing SHA256SUMS.txt   # macOS
```

```powershell
(Get-FileHash LeanCut-Windows.zip -Algorithm SHA256).Hash   # Windows: compare with the line in SHA256SUMS.txt
```

GitHub also shows a `sha256:` digest next to every release asset. Both come from the same GitHub release, so they
catch a broken download, not a compromised account. Signing the checksums (for example with Sigstore/cosign or
GitHub artifact attestations) would be the next step if that matters.
