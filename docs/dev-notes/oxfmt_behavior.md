# oxfmt behavior

Observed with: npx -y oxfmt@latest --version
    Version: 0.50.0
    

## formatted.ts

Command: npx -y oxfmt@latest --check formatted.ts
Exit: 0

~~~
Checking formatting...

All matched files use the correct format.
Finished in 26ms on 1 files using 16 threads.
No config found, using defaults. Please add a config file or try `oxfmt --init` if needed.
~~~

## drifted.ts

Command: npx -y oxfmt@latest --check drifted.ts
Exit: 1

~~~
Checking formatting...

/var/folders/12/nd4g1y_j14zc1bt2l3c2pk900000gn/T/tmp.jSyI1DSIIX/drifted.ts (0ms)

Format issues found in above 1 files. Run without `--check` to fix.
Finished in 20ms on 1 files using 16 threads.
No config found, using defaults. Please add a config file or try `oxfmt --init` if needed.
~~~

## types.d.ts

Command: npx -y oxfmt@latest --check types.d.ts
Exit: 1

~~~
Checking formatting...

/var/folders/12/nd4g1y_j14zc1bt2l3c2pk900000gn/T/tmp.jSyI1DSIIX/types.d.ts (0ms)

Format issues found in above 1 files. Run without `--check` to fix.
Finished in 57ms on 1 files using 16 threads.
No config found, using defaults. Please add a config file or try `oxfmt --init` if needed.
~~~

## types-formatted.d.ts

Command: npx -y oxfmt@latest --check types-formatted.d.ts
Exit: 0

~~~
Checking formatting...

All matched files use the correct format.
Finished in 22ms on 1 files using 16 threads.
No config found, using defaults. Please add a config file or try `oxfmt --init` if needed.
~~~

