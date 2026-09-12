import '@backstage/cli/asset-types';
import ReactDOM from 'react-dom/client';
import App from './App';
import '@backstage/ui/css/styles.css';
// After Backstage UI's styles, so the Dev Portal values win (see the file).
import './modules/devPortal.css';

ReactDOM.createRoot(document.getElementById('root')!).render(App.createRoot());
