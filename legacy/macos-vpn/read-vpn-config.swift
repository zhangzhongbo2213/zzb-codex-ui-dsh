import Foundation
import Security
import LocalAuthentication
import CryptoKit
let base = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/org.ikuuu.vpn")
let bytes = try Data(contentsOf: base.appendingPathComponent("flutter_secure_storage.dat"))
let envelope = try JSONSerialization.jsonObject(with: bytes) as! [String: Any]
let context = LAContext()
context.interactionNotAllowed = true
let query: [String: Any] = [kSecClass as String: kSecClassKey, kSecAttrApplicationTag as String: Data("org.ikuuu.vpn.secure-storage.rsa.v1".utf8), kSecAttrKeyType as String: kSecAttrKeyTypeRSA, kSecReturnRef as String: true, kSecUseAuthenticationContext as String: context]
var item: CFTypeRef?
let status = SecItemCopyMatching(query as CFDictionary, &item)
guard status == errSecSuccess, let item = item else {
 print("KEYCHAIN_STATUS \(status)")
 exit(2)
}
let privateKey = item as! SecKey
let wrapped = Data(base64Encoded: envelope["encryptedKey"] as! String)!
var error: Unmanaged<CFError>?
guard let plaintextKey = SecKeyCreateDecryptedData(privateKey, .rsaEncryptionOAEPSHA256, wrapped as CFData, &error) as Data? else {
 print("KEY_UNWRAP_FAILED")
 exit(3)
}
let combined = Data(base64Encoded: envelope["payload"] as! String)!
let plaintext = try AES.GCM.open(AES.GCM.SealedBox(combined: combined), using: SymmetricKey(data: plaintextKey))
let map = try JSONSerialization.jsonObject(with: plaintext) as! [String: Any]

let value = map["riverpod_persist:ikuuu_subscription"] as! String
let parsed = try JSONSerialization.jsonObject(with: Data(value.utf8))

let outer = parsed as! [String: Any]
let inner = try JSONSerialization.jsonObject(with: Data((outer["value"] as! String).utf8)) as! [String: Any]
let content = inner["content"] as! String
let target = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("local/share/zzb-codex-vpn/subscription.private")
try Data(content.utf8).write(to: target, options: .atomic)
try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: target.path)
print("Subscription exported to private staging file")
