/**
 * Sign-in from the service mesh: the `rctoken` auth provider.
 *
 * Behind Anthos Service Mesh user auth (authservice), the person signs in with
 * the company's identity provider AT THE MESH; every request that reaches a
 * workload then carries a signed RCToken in a header. This provider turns that
 * token into a Backstage identity — after verifying it, never by decoding it:
 * the signature against the issuer's JWKS, the issuer, the audience and the
 * expiry, exactly as the release agent does (src/release_agent/identity.py).
 * A header is a claim; a verified token is an identity.
 *
 *   auth:
 *     providers:
 *       rctoken:
 *         header: x-rctoken                  # where authservice puts the token
 *         jwksUrl: http://authservice.asm-user-auth.svc.cluster.local:10004/_gcp_user_auth/jwks
 *         issuer: authservice.asm-user-auth.svc.cluster.local
 *         audience: <the UserAuthConfig audience>
 *         emailClaim: attributes.email,email # dotted paths, first one present wins
 *         signIn:
 *           resolvers:
 *             - resolver: emailLocalPartMatchingUserEntityName
 *               dangerouslyAllowSignInWithoutUserInCatalog: true
 *
 * The front end uses it when `app.meshSignIn: true` (packages/app/src/modules/signIn.tsx).
 */
import { createBackendModule } from '@backstage/backend-plugin-api';
import {
  authProvidersExtensionPoint,
  commonSignInResolvers,
  createProxyAuthProviderFactory,
  createProxyAuthenticator,
} from '@backstage/plugin-auth-node';
import { AuthenticationError } from '@backstage/errors';
import { createRemoteJWKSet, jwtVerify, JWTPayload } from 'jose';

export type RcTokenResult = { email: string; name?: string };

type Context = {
  header: string;
  issuer: string;
  audience?: string;
  emailClaims: string[];
  jwks: ReturnType<typeof createRemoteJWKSet>;
};

/** The value at a dotted path ("attributes.email") of the token's claims. */
export function claimAt(payload: JWTPayload, path: string): unknown {
  let at: unknown = payload;
  for (const part of path.split('.')) {
    if (!at || typeof at !== 'object') return undefined;
    at = (at as Record<string, unknown>)[part];
  }
  return at;
}

/** The first configured email claim that holds an email-looking string. */
export function emailOf(payload: JWTPayload, paths: string[]): string | undefined {
  for (const path of paths) {
    const value = claimAt(payload, path);
    if (typeof value === 'string' && value.includes('@')) return value.trim().toLowerCase();
  }
  return undefined;
}

export const rcTokenAuthenticator = createProxyAuthenticator<Context, RcTokenResult, unknown>({
  defaultProfileTransform: async result => ({
    profile: { email: result.email, displayName: result.name ?? result.email },
  }),
  initialize({ config }) {
    const jwksUrl = config.getString('jwksUrl');
    return {
      header: config.getOptionalString('header') ?? 'x-rctoken',
      issuer: config.getString('issuer'),
      audience: config.getOptionalString('audience'),
      emailClaims: (config.getOptionalString('emailClaim') ?? 'attributes.email,email')
        .split(',')
        .map(s => s.trim())
        .filter(Boolean),
      // jose caches the keys and refetches on an unknown kid (key rotation).
      jwks: createRemoteJWKSet(new URL(jwksUrl)),
    };
  },
  async authenticate({ req }, ctx) {
    const raw = req.header(ctx.header);
    if (!raw) {
      throw new AuthenticationError(
        `No ${ctx.header} header — this Backstage expects to sit behind the mesh's user auth.`,
      );
    }
    const token = raw.startsWith('Bearer ') ? raw.slice(7) : raw;
    let payload: JWTPayload;
    try {
      ({ payload } = await jwtVerify(token, ctx.jwks, {
        issuer: ctx.issuer,
        ...(ctx.audience ? { audience: ctx.audience } : {}),
      }));
    } catch (e) {
      throw new AuthenticationError(`The mesh identity token could not be verified: ${(e as Error).message}`);
    }
    const email = emailOf(payload, ctx.emailClaims);
    if (!email) {
      throw new AuthenticationError(`The mesh identity token carries no email (${ctx.emailClaims.join(', ')}).`);
    }
    const name = claimAt(payload, 'attributes.name') ?? claimAt(payload, 'name');
    return { result: { email, name: typeof name === 'string' ? name : undefined } };
  },
});

export const rcTokenAuthModule = createBackendModule({
  pluginId: 'auth',
  moduleId: 'rctoken-provider',
  register(reg) {
    reg.registerInit({
      deps: { providers: authProvidersExtensionPoint },
      async init({ providers }) {
        providers.registerProvider({
          providerId: 'rctoken',
          factory: createProxyAuthProviderFactory({
            authenticator: rcTokenAuthenticator,
            signInResolverFactories: { ...commonSignInResolvers },
          }),
        });
      },
    });
  },
});
