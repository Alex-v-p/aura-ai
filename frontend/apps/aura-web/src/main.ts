import { isDevMode } from '@angular/core';
import { bootstrapApplication } from '@angular/platform-browser';
import { provideAnimationsAsync } from '@angular/platform-browser/animations/async';
import { provideRouter } from '@angular/router';
import { AppComponent } from './app/app.component';
import { routes } from './app/app.routes';

bootstrapApplication(AppComponent, {
  providers: [provideRouter(routes), provideAnimationsAsync()],
})
  .then(() => {
    if (!isDevMode() && 'serviceWorker' in navigator) {
      navigator.serviceWorker.register('/sw.js').catch((error: unknown) => console.warn('Aura service worker unavailable', error));
    }
  })
  .catch((error: unknown) => console.error(error));
