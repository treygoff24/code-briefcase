# tsc --allowJs no-config behavior

Observed with:
    Version 6.0.3

## valid.js

Command: npx -y -p typescript@latest tsc --noEmit --allowJs --pretty false valid.js
Exit: 0

~~~

~~~

## type_error.js

Command: npx -y -p typescript@latest tsc --noEmit --allowJs --pretty false type_error.js
Exit: 2

~~~
type_error.js(3,7): error TS2322: Type 'number' is not assignable to type 'string'.
~~~

## component.jsx

Command: npx -y -p typescript@latest tsc --noEmit --allowJs --pretty false component.jsx
Exit: 0

~~~

~~~

