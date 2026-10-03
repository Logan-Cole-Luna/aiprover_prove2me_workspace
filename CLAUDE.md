Do not write any — ever

When responding and writing documentation use language that is concise, scientific, and professional. For example no "what this pays". Also in documents do not discuss past failures and fixes, those are not meant to be documented. Treat docs as you would a research paper/report

Whenever running a proccess with a temp file, output it to temp/ so I can observe the run in realtime as well, when it is no longer useful delete it. 

The same idea as above applys to the logs/ folder, every so often delete old logs no longer of use to us

Update the README.md as we go along, include an overview of the repo (with a mermaid diagram as well) and also include current results, keep the readme concise though as too much will confuse a reader

Also update logbook.md with Todo, Done, Decisions, and Issues

On an HPC, for anything compute heavy always submit a slurm job, login nodes are resource limited and monitored. Lightweight processes can be run on the node.


use https://mitcommlab.mit.edu/broad/commkit/coding-and-comment-style/ as a comment guideline:

Why code needs style

Think about a time you were working on a piece of code and got frustrated trying to debug or modify it, or a time you struggled to figure out how to use code that someone else wrote. Almost surely, the difficulty was that the logic of the code was not being effectively communicated.

This article will discuss how to use effective naming, structuring, context, and comments to communicate your logic in an easy-to-use code. Coding styles come in many shapes and sizes, but good ones derive from the same fundamental principles and possess a few key properties. Understanding these shared properties will help you choose a style that improves your code development, while also making it easier for others to implement.

Before jumping in, remember that styles are conventions, so if your project or lab group has an existing scheme—stick with it. But maybe you’ll have some ideas for improving it after reading this guide.
Your audience is the human who will do something useful with your code.

Your code will only be used if your target audience understands it, which means that your style should be simple, clear, and consistent. Beginners need clarity and minimal distractions as they learn, while those with more experience expect your code to be consistent with field conventions. Let your logic be the innovation, not the variable names or file structure.

Keep in mind how common it is for code snippets to be borrowed in unexpected ways. Whenever possible, write for a broad audience to avoid limiting the code’s potential impact.
Write clear code before adding comments.

The most important concept here is that the code itself is the primary means of commenting and documentation. Within 100 or 1000 lines of code, there is a wealth of opportunity to embed your intended meaning and use context to communicate with your audience. Adding more supporting literature to compensate for confusing code usually just makes it more tedious and difficult to work with the code.

Four principal mechanisms govern clear code communication: naming, structure, context, and comments. Below, we recommend practices and show examples for each.
1. Naming
Practice 	Recommendations
Call things what they are. 	Keep names straightforward and descriptive. Be concise, but avoid abbreviations unless they are self-explanatory.

A few rules of thumb:

    Variables/classes are nouns.
    Functions/methods are verbs.
    Try to keep names pronounceable.
    Name lengths should be proportional to the variable’s scope.

Define ambiguous names. 	Sometimes constraints just leave no good way around abbreviations. Add a comment in natural English explaining the name.

Examples of ineffective and effective naming

% These variable names probably have no meaning to you... d=1000; % Is this a diameter maybe? vsc=le-6; %These names probably do... %And in a fluid flow solver, we may use context and controversial symbols to %avoid ambiguity on which viscosity we're using, also keeping names concise: rho=1000; %[kg/m3] (conventional symbol for density, specifying units) nu=le-6; %[m2/sec] (kinematic viscosity) mu=nu*rho; %[Pa*sec] (dynamic viscosity C Sometimes abbreviations can't be avoided. FORTRAN 77 has a 6-character limit for names and is case-insensitive, so to avoid utter confusion, we'll define the short name in a comment: C PHI_I1 = first order incident wave velocity potential C PHI_I2 = second order incident wave velocity potential PHI_I1 = ... some math ... PHI_I2 = ... some uglier math...

Consistent and purposeful typography also helps make names readable and distinguishes different code elements (variables, types, functions, etc.). Nearly all languages have their typographic preferences; the table below summarizes a few common styles, and more comprehensive guides are linked in the Appendix at the end of this article. In shorter codes or with fewer collaborators, it’s okay to break away from your language’s style if something else works better for you, as long as you stick to it.

    	Constants 	Variables 	Types (Structures) 	Functions 	Modules
    Python 	lower_case 	lower_case 	CamelCase 	lower_case 	lowercase
    MATLAB 	UPPERCASE 	mixedCase 	CamelCase 	lowercase 	—
    Julia 	lowercase 	lower_case 	CamelCase 	lower_case 	CamelCase
    C++ 	kCamelCase 	lower_case 	CamelCase 	CamelCase 	lower_case
    Fortran 90 	lowercase 	lower_case 	lower_case 	lower_case 	lowercase
    Java 	UPPER_CASE 	CamelCase 	CamelCase 	mixedCase 	lowercase
    R 	lower_case 	lower_case 	lower_case 	lower_case 	—

2. Structure and Alignment

Structure improves the readability of the code, and groups related ideas together.  Use two practices to structure effectively:
Practice 	Recommendations
Structure vertically, not horizontally. 	Humans prefer to read columns—think about effective newspaper and website layouts. Additionally, remember that the interesting part of a statement happens on the right end. So if a statement runs off the right edge of the screen, it’s easy to miss the importance of what’s going on, either because you form judgements too quickly or get lost endlessly scrolling right. Stick to these principles:

    Don’t go beyond column 80 (unless you have a really good reason),
    Only put one statement per line,
    Indent with spaces (most languages use 4), never use tabs (tip: configure the tab key in your text editor to add 4 spaces),
    Indent once within each loop or function,
    Indent line continuations once,
    Place the operator where you break a line on the left of the new line.

Group related objects and uses. 	Identify things that belong together under one purpose, and use alignment to display that grouping.

    For example, consider the argument list of a function, which is a single concept used by a function call. If it can fit on one 80-column line, go for it.
    If not, there are many ways to break and align it over multiple lines.
    While there are many ways to do it, a great choice is the alignment style shown in the second function call in this example, because it has two important properties.
        It groups the single argument list concept into one block…
        …which is also invariant under refactoring. Alignment styles that are not invariant under refactoring will get mutilated and require extra maintenance to maintain readability if, for example, the name of the function is changed.

To see an extended example of improving code through strategic alignment and grouping decisions, click here.
A note on the mechanics of spacing

Spacing paradigms are similar over the common languages and follow standard English grammatical conventions:

    One space after a comma, none before,
    One space on either side of an assignment operator (e.g. “x = 3“),
    One space to either side of a boolean operator (e.g. “x < y“),
    Either one space on each side of a boolean operation, or none (e.g. “1+1” or “1 + 1”; never “1+ 1” or mixing spaces and no-spaces in a sourcefile),
    Add them where it aids readability.

3. Context

Use context to avoid redundancy in names or comments. For example, in a catch statement, “argumentException” is a redundant and ambiguous name. It’s redundant because catch blocks are used to handle exceptions, and it’s ambiguous because it’s not clear what about the argument is being caught.

A better name would be “invalidArgument,” or, for a function that requires two n-by-n matrices, “argumentDimensionMismatch.”
4. Comments

Use comments only to add context or explain choices that cannot be expressed through thoughtful naming, structure or the context of operations. Keep in mind that comments will most commonly be used by another developer, user, or yourself down the line, but…

“Comments are the last thing people will look at when everything has gone wrong. It’s like getting a new gadget, the last thing you’re going to do is actually look at the instructions if you’re a geek.” —Kevlin Henney

Comments are always complete sentences. Use them, but don’t abuse them—remember to make comments meaningful. For example, a comment like this doesn’t add much:
% Add two numbers (x and y) together.
function z = adder(x,y)
z = x+y;
end

You can tailor comments for specific audience members. Frequently, they’re targeted at other developers to keep track of tasks and decisions. Be specific—describe the problem, what needs to be done, and who is responsible for addressing it, e.g.:

This is a bad TODO comment:
% TODO: make it work.

This is a good TODO comment:
% TODO: (@David) implement 2-step predictor-corrector integrator for
% stability. Shocks develop when using Adams-Bashforth integration.

Keep in mind there is a cost to adding comments—just as with the source code, comments require maintenance. There’s nothing more frustrating than a misleading, outdated comment that doesn’t match up with what the code is actually doing.