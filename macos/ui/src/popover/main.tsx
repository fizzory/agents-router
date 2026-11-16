import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import * as api from '../../tauri.js';
import type { PopoverApi } from './api';
import { PopoverApp } from './PopoverApp';

const adapter: PopoverApi = api;
const root = document.getElementById('root');
if (!root) throw new Error('popover.html has no #root');
createRoot(root).render(<StrictMode><PopoverApp api={adapter} /></StrictMode>);
