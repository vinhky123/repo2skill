export function createClient(baseUrl: string): string {
  return fetch(`${baseUrl}/api`);
}
