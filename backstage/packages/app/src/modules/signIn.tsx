import { ComponentType } from 'react';
import { createFrontendModule } from '@backstage/frontend-plugin-api';
import { SignInPageBlueprint } from '@backstage/plugin-app-react';
import { configApiRef, useApi } from '@backstage/core-plugin-api';
import { ProxiedSignInPage, SignInPage } from '@backstage/core-components';
import type { SignInPageProps } from '@backstage/core-plugin-api';

/**
 * Who you are comes from the mesh, not a login form in Backstage.
 *
 * Behind Anthos Service Mesh user auth (authservice), the person has already
 * signed in with the company's identity provider at the ingress; every request
 * then carries a signed RCToken. With `app.meshSignIn: true` the page signs in
 * through the backend's `rctoken` provider, which verifies that token — the
 * same token, the same checks, as the release agent — so the person is simply
 * "signed in as alice@example.com", no button to press. Without it (local
 * development, no mesh) it is the usual Guest page.
 */
function MeshOrGuestSignIn(props: SignInPageProps) {
  const config = useApi(configApiRef);
  if (config.getOptionalBoolean('app.meshSignIn')) {
    return <ProxiedSignInPage {...props} provider="rctoken" />;
  }
  return <SignInPage {...props} providers={['guest']} />;
}

export const signInModule = createFrontendModule({
  pluginId: 'app',
  extensions: [
    SignInPageBlueprint.make({
      params: {
        loader: async (): Promise<ComponentType<SignInPageProps>> => MeshOrGuestSignIn,
      },
    }),
  ],
});
