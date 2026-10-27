import type {
  CredentialProfileInput,
  CredentialTestResponse,
  CredentialsResponse,
  NodeCredentialGenerateResponse,
  SshTestInput,
} from "@/types"
import { fetchJson, postJson, putJson } from "@/lib/api"

export function loadCredentials(signal?: AbortSignal) {
  return fetchJson<CredentialsResponse>("/api/credentials", signal)
}

export function saveCredentialProfile(profileId: string, input: CredentialProfileInput) {
  return putJson<CredentialsResponse>(`/api/credentials/profiles/${encodeURIComponent(profileId)}`, input)
}

export function testCredentialProfile(profileId: string, input: CredentialProfileInput) {
  return postJson<CredentialTestResponse>(`/api/credentials/profiles/${encodeURIComponent(profileId)}/test`, input)
}

export function testSshConnection(input: SshTestInput) {
  return postJson<CredentialTestResponse>("/api/credentials/test/ssh", input)
}

export function generateNodeCredentials() {
  return postJson<NodeCredentialGenerateResponse>("/api/credentials/node-credentials/generate", {})
}
