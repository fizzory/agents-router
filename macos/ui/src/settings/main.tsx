import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import * as api from '../../tauri.js';
import type { SettingsApi } from './api';
import { SettingsApp } from './SettingsApp';

const adapter = api as unknown as SettingsApi;
const root = document.getElementById('root');
if (!root) throw new Error('settings.html has no #root');
createRoot(root).render(<StrictMode><SettingsApp api={adapter} /></StrictMode>);
