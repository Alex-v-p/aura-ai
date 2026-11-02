import { afterEach, describe, expect, it } from 'vitest';
import { focusNotificationReturnTarget } from './notification-focus';

describe('notification detail return focus', () => {
  afterEach(() => { document.body.innerHTML = ''; });

  it('focuses the Notifications heading when acknowledgement removed the opener', () => {
    document.body.innerHTML = '<h1 id="notifications-heading" tabindex="-1">Notifications</h1><button id="notification-1">Open</button>';
    document.getElementById('notification-1')?.remove();

    focusNotificationReturnTarget('notification-1');

    expect(document.activeElement?.id).toBe('notifications-heading');
  });

  it('keeps the original opener as the preferred close target when it remains', () => {
    document.body.innerHTML = '<h1 id="notifications-heading" tabindex="-1">Notifications</h1><button id="notification-1">Open</button>';

    focusNotificationReturnTarget('notification-1');

    expect(document.activeElement?.id).toBe('notification-1');
  });
});
