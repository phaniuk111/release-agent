import { createLocalJWKSet, exportJWK, generateKeyPair, SignJWT } from 'jose';
import { claimAt, emailOf, rcTokenAuthenticator } from './rctokenAuth';

const ISSUER = 'authservice.asm-user-auth.svc.cluster.local';
const AUDIENCE = 'release-portal';

async function setup() {
  const { publicKey, privateKey } = await generateKeyPair('RS256');
  const jwk = { ...(await exportJWK(publicKey)), kid: 'k1', alg: 'RS256' };
  const ctx = {
    header: 'x-rctoken',
    issuer: ISSUER,
    audience: AUDIENCE,
    emailClaims: ['attributes.email', 'email'],
    jwks: createLocalJWKSet({ keys: [jwk] }) as any,
  };
  const sign = (claims: Record<string, unknown>, opts: { issuer?: string; key?: any } = {}) =>
    new SignJWT(claims)
      .setProtectedHeader({ alg: 'RS256', kid: 'k1' })
      .setIssuer(opts.issuer ?? ISSUER)
      .setAudience(AUDIENCE)
      .setIssuedAt()
      .setExpirationTime('5m')
      .sign(opts.key ?? privateKey);
  const req = (headers: Record<string, string>) => ({ header: (name: string) => headers[name.toLowerCase()] }) as any;
  return { ctx, sign, req };
}

describe('rctoken sign-in', () => {
  it('reads the email from the first configured claim path that holds one', () => {
    const payload = { attributes: { email: 'Alice@Example.com', name: 'Alice' }, email: 'other@example.com' };
    expect(claimAt(payload, 'attributes.name')).toBe('Alice');
    expect(emailOf(payload, ['attributes.email', 'email'])).toBe('alice@example.com');
    expect(emailOf({ email: 'bob@example.com' }, ['attributes.email', 'email'])).toBe('bob@example.com');
    expect(emailOf({ attributes: { email: 'not-an-email' } }, ['attributes.email'])).toBeUndefined();
  });

  it('signs in a person whose mesh token verifies', async () => {
    const { ctx, sign, req } = await setup();
    const token = await sign({ attributes: { email: 'alice@example.com', name: 'Alice Example' } });
    const got = await rcTokenAuthenticator.authenticate({ req: req({ 'x-rctoken': token }) }, ctx);
    expect(got.result).toEqual({ email: 'alice@example.com', name: 'Alice Example' });
    const profile = await rcTokenAuthenticator.defaultProfileTransform(got.result, {} as any);
    expect(profile.profile).toEqual({ email: 'alice@example.com', displayName: 'Alice Example' });
  });

  it('refuses a token from another issuer, a forged one, and none at all', async () => {
    const { ctx, sign, req } = await setup();
    const foreign = await sign({ email: 'alice@example.com' }, { issuer: 'someone-else' });
    await expect(rcTokenAuthenticator.authenticate({ req: req({ 'x-rctoken': foreign }) }, ctx)).rejects.toThrow(
      /could not be verified/,
    );
    const other = await generateKeyPair('RS256');
    const forged = await sign({ email: 'alice@example.com' }, { key: other.privateKey });
    await expect(rcTokenAuthenticator.authenticate({ req: req({ 'x-rctoken': forged }) }, ctx)).rejects.toThrow(
      /could not be verified/,
    );
    await expect(rcTokenAuthenticator.authenticate({ req: req({}) }, ctx)).rejects.toThrow(/No x-rctoken header/);
  });

  it('refuses a verified token that names nobody', async () => {
    const { ctx, sign, req } = await setup();
    const token = await sign({ sub: '12345' });
    await expect(rcTokenAuthenticator.authenticate({ req: req({ 'x-rctoken': token }) }, ctx)).rejects.toThrow(
      /carries no email/,
    );
  });
});
