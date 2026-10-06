export interface Config {
  app?: {
    /**
     * Sign in from the service mesh's verified identity (the `rctoken` auth
     * provider) instead of the Guest page. Set where Backstage runs behind
     * Anthos Service Mesh user auth.
     * @visibility frontend
     */
    meshSignIn?: boolean;
  };
}
