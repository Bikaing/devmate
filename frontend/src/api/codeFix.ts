import http from './http';

export type VerificationProfile = 'pytest' | 'unittest' | 'npm-build';
export interface VerificationResult {
  argv: string[];
  exit_code: number | null;
  status: string;
  output: string;
  duration_ms: number;
  error: string | null;
}
export interface FixAttempt {
  attempt_number: number;
  status: string;
  patch: string | null;
  verification_status: string;
  verification_results: VerificationResult[];
  error_message: string | null;
  duration_ms: number | null;
}
export interface FixTask {
  id: string;
  status: string;
  attempt_count: number;
  max_attempts: number;
  verification_profile: VerificationProfile;
  verification_status: string;
  baseline_results: VerificationResult[];
  verification_results: VerificationResult[];
  patch: string | null;
  changed_files: string[];
  error_message: string | null;
  cleanup_error: string | null;
  created_at: string;
  attempts?: FixAttempt[];
}
export async function startFix(findingId: string, profile: VerificationProfile) {
  const { data } = await http.post<{ id: string }>(`/code-fix/findings/${findingId}/tasks`, {
    verification_profile: profile,
  });
  return data;
}
export async function listFixes(findingId: string) {
  return (await http.get<FixTask[]>(`/code-fix/findings/${findingId}/tasks`)).data;
}
export async function getFix(taskId: string) {
  return (await http.get<FixTask>(`/code-fix/tasks/${taskId}`)).data;
}
export async function cancelFix(taskId: string) {
  await http.post(`/code-fix/tasks/${taskId}/cancel`);
}
export async function applyFix(taskId: string) {
  await http.post(`/code-fix/tasks/${taskId}/apply`, { confirmed: true });
}
