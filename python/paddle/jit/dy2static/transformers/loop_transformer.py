#   Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
from collections import defaultdict

from paddle.base import unique_name
from paddle.utils import gast

from ..utils import (
    FOR_BODY_PREFIX,
    FOR_CONDITION_PREFIX,
    WHILE_BODY_PREFIX,
    WHILE_CONDITION_PREFIX,
    FunctionNameLivenessAnalysis,
    GetterSetterHelper,
    ast_to_source_code,
    create_get_args_node,
    create_name_str,
    create_nonlocal_stmt_nodes,
    create_set_args_node,
    get_attribute_full_name,
    get_parent_mapping,
)
from .base import (
    BaseTransformer,
    ForLoopTuplePreTransformer,
    ForNodeVisitor,
)
from .ifelse_transformer import ARGS_NAME

__all__ = []


def create_while_nodes(
    condition_name,
    body_name,
    loop_var_names,
    push_pop_names,
    getter_name,
    setter_name,
):
    """
    Returns a list of gast.Node which represents the calling of Paddle
    controlflow while_loop.

    Usually, the list just contain 1 statement such as:

    [a, b, c] = paddle.jit.dy2static.convert_while_loop(
            condition_name, body_name, [a, b, c])

    where a, b, c are in loop_var_names.

    However, if loop_var_names contains property such as foo.x, we cannot
    assign the property as output of convert_while_loop because Python
    property is a kind of read-only attribute. To handle the case, we replace
    the attributes which are output of convert_while_loop with generated
    variables, then if we know the attribute is not read-only at runtime, we
    assign the attribute. The created statements are like:

    [a, b, __attribute_variable_1] = paddle.jit.dy2static.convert_while_loop(
            condition_name, body_name, [a, b, foo.x])
    if not isinstance(getattr(type(foo), x, None), property): foo.x = __attribute_variable_1

    The number of above statements is not only 1, that's why the return type is
    a list of gast.Node.
    """
    # NOTE(liym27):
    # It's better to parse the source code into an AST node than to customize an AST node
    # including child nodes, because it is easy to mistake the ast node type when customizing the node.
    #
    # For example: loop_var_names = [a, b, foo.x], the type of `a` or `b` is gast.Name,
    # but the type of `foo.x` gast.Attribute.
    # We have to make loop_var_names and assign_loop_var_names with same order
    # set doesn't have order so we convert it to list
    loop_var_names = list(loop_var_names)
    assign_loop_var_names = []
    for name in loop_var_names:
        assign_loop_var_names.append(name)

    while_func_name = "_jst.While"
    while_node_str = (
        "{}({}, {}, {}, {}, return_name_ids={}, push_pop_names={})".format(
            while_func_name,
            condition_name,
            body_name,
            getter_name,
            setter_name,
            create_name_str(loop_var_names),
            create_name_str(push_pop_names),
        )
    )
    while_node = gast.parse(while_node_str).body[0]

    ret = [while_node]
    return ret


class NameVisitor(gast.NodeVisitor):
    '''
    Analysis name liveness for loop transformer
    '''

    def __init__(self, root_node):
        # Set of gast.Name or gast.Attribute for variables
        self.current_seen_vars = set()

        # List of gast.While/gast.For nodes
        self.current_loop = []

        # List of nodes that have scope of variables.
        self.nodes_with_scope = []
        self.blacklist_names = {"False", "True", "None"}

        # Mapping from gast.While/gast.For to variable nodes
        self.before_loop_body_vars = defaultdict(set)
        # NOTE: Use ordered list as dict value
        self.in_loop_vars = defaultdict(list)

        # Mapping from gast.While/gast.For to variable nodes which is condition
        # of loop or being modified during the loop
        self.write_in_loop = defaultdict(set)
        self.condition_vars = defaultdict(set)
        self.in_condition = False

        # Some names are types, we shouldn't record them as loop var names.
        self.type_vars = set()

        self.to_parent_mapping = get_parent_mapping(root_node)

        self.visit(root_node)

    def get_loop_var_names(self, node):
        assert isinstance(
            node, (gast.While, gast.For)
        ), "Input node is not gast loop node"
        loop_var_names = set()
        create_var_names = set()
        read_context = {type(gast.Load()), type(gast.AugLoad())}

        in_loop_vars_list = self.in_loop_vars[node]

        # get dict `var_name_to_ctxs`
        var_name_to_ctxs = defaultdict(list)
        for var_node in in_loop_vars_list:
            var_name_to_ctxs[self._var_node_to_name(var_node)].append(
                var_node.ctx
            )

        in_loop_vars = set(in_loop_vars_list)
        in_loop_vars = self._remove_unnecessary_vars(in_loop_vars, node)
        in_loop_name_strs = self._var_nodes_to_names(in_loop_vars)

        before_loop_body_vars = self.before_loop_body_vars[node]
        before_loop_body_vars = self._remove_unnecessary_vars(
            before_loop_body_vars, node
        )
        before_loop_name_strs = self._var_nodes_to_names(before_loop_body_vars)

        after_loop_vars = (
            self.current_seen_vars - before_loop_body_vars - in_loop_vars
        )
        after_loop_vars = self._remove_unnecessary_vars(after_loop_vars, node)
        after_loop_name_strs = self._var_nodes_to_names(
            after_loop_vars, read_context
        )
        condition_vars = self.condition_vars[node]
        condition_names = self._var_nodes_to_names(condition_vars)

        write_vars = self.write_in_loop[node]
        write_names = self._var_nodes_to_names(write_vars)

        for name in in_loop_name_strs:
            if name in before_loop_name_strs:
                # If a variable is used in loop and created before loop

                # If this var is a basic variable and read-only and not
                # condition var, it may not be loop_var else it should
                # be in loop_var as input
                if (name not in condition_names) and (name not in write_names):
                    continue
                loop_var_names.add(name)

            elif name in after_loop_name_strs:
                # If a variable is created in the while loop and read after
                # loop, it should be in loop_var and we should create it

                # because name in after_loop_name must be initialized in loop
                # So it is write-only, we don't have to filter read-only basic
                # vars out
                loop_var_names.add(name)
                create_var_names.add(name)
            else:
                # If a variable is used and created in loop, but used before created,
                # it should be in loop_var and we should create it.

                # For example, `var_a` should be in loop_var and we should create it.
                #
                #   res = 0
                #   for i, x in enumerate(x_array):
                #       if i > 2:
                #           x = func1(var_a)
                #       var_a = func2(x)
                #

                is_created = False
                for ctx in var_name_to_ctxs[name]:
                    if isinstance(ctx, gast.Store):
                        is_created = True

                if (
                    isinstance(var_name_to_ctxs[name][0], gast.Load)
                    and is_created
                ):
                    loop_var_names.add(name)
                    create_var_names.add(name)

        return loop_var_names, create_var_names

    def visit_Name(self, node):
        if self._is_call_func_name_node(node):
            self.generic_visit(node)
            return
        if node.id in self.blacklist_names:
            self.generic_visit(node)
            return

        self.current_seen_vars.add(node)
        write_context = {
            type(gast.Store()),
            type(gast.AugStore()),
            type(gast.Del()),
        }

        for loop_node in self.current_loop:
            self.in_loop_vars[loop_node].append(node)
            if type(node.ctx) in write_context:
                self.write_in_loop[loop_node].add(node)
        if self.in_condition:
            self.condition_vars[loop_node].add(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        self.nodes_with_scope.append(node)
        self.blacklist_names.add(node.name)

        # The variables in the function are not visible to the outside scope.
        before_func_seen_vars = copy.copy(self.current_seen_vars)

        self.generic_visit(node)
        self.nodes_with_scope.pop()
        # After exiting the scope of the node, variables in this scope
        # should be removed from self.current_seen_vars.
        if self.nodes_with_scope:
            self.current_seen_vars = before_func_seen_vars

    def visit(self, node):
        method = 'visit_' + node.__class__.__name__
        visitor = getattr(self, method, self.generic_visit)
        ret = visitor(node)
        return ret

    def visit_Attribute(self, node):
        if self._is_call_func_name_node(node):
            return
        attr_full_name = get_attribute_full_name(node)
        # Class variables are not allowed to appear in the arguments list
        # of defined function under class methods in Python.
        """
        def class_func(self):
            def while_loop_body(self.x, y) # `self.x` is illegal.
        """
        # TODO: If do change the variable with `self.var`, need a better
        # way to deal with this case.
        if attr_full_name.startswith("self."):
            return
        self.current_seen_vars.add(node)

        for loop_node in self.current_loop:
            self.in_loop_vars[loop_node].append(node)

        # sub-nodes are visited during get_attribute_full_name and we shouldn't
        # visit again

    def visit_For(self, node):
        self.current_loop.append(node)
        self.in_condition = True
        self.visit(node.target)
        self.visit(node.iter)
        self.in_condition = False
        self.before_loop_body_vars[node] = copy.copy(self.current_seen_vars)
        self.generic_visit(node)
        self.current_loop.pop()

    def visit_While(self, node):
        self.current_loop.append(node)
        self.in_condition = True
        self.visit(node.test)
        self.in_condition = False
        self.before_loop_body_vars[node] = copy.copy(self.current_seen_vars)
        self.generic_visit(node)
        self.current_loop.pop()

    def visit_Call(self, node):
        # Store type var names such as "isinstance(x, some_type_names)" and
        # Remove them later
        if isinstance(node.func, gast.Name) and node.func.id == 'isinstance':
            type_node = node.args[1]
            if isinstance(type_node, gast.Tuple):
                for element in type_node.elts:
                    self.type_vars.add(ast_to_source_code(element).strip())
            else:
                self.type_vars.add(ast_to_source_code(type_node).strip())
        self.generic_visit(node)

    def _var_nodes_to_names(self, node_set, ctx_filter_set=None):
        ret = set()
        for node in node_set:
            if ctx_filter_set is None or type(node.ctx) in ctx_filter_set:
                ret.add(self._var_node_to_name(node))
        return ret

    def _var_node_to_name(self, node):
        if isinstance(node, gast.Name):
            return node.id
        elif isinstance(node, gast.Attribute):
            return get_attribute_full_name(node)

    def _is_call_func_name_node(self, node):
        parent_node = self._get_parent_node(node)
        if isinstance(parent_node, gast.Call) and parent_node.func == node:
            return True
        return False

    def _is_global_or_nonlocal(self, node):
        return False

    def _is_ancestor_node(self, ancestor_node, node):
        parent_node = self._get_parent_node(node)

        while parent_node is not None:
            if parent_node == ancestor_node:
                return True
            parent_node = self._get_parent_node(parent_node)
        return False

    def _get_parent_node(self, node):
        return self.to_parent_mapping.get(node)

    def _remove_unnecessary_vars(self, loop_vars, loop_node):
        """
        Remove unnecessary vars from before_loop_vars, after_loop_vars or in_loop_vars about loop_node.
            1. Remove target vars of gast.For from before_loop_vars or after_loop_vars.
            2. Remove vars only in gast.comprehension.
            3. Remove vars that are type names, for example: "isinstance(x, var_type_name)"
        :param loop_vars: before_loop_vars, after_loop_vars or in_loop_vars of loop_node.
        :param loop_node: Current loop node.
        """

        def filter_name_nodes_from(root_node, target_var_names):
            """
            Filter children with gast.Name type from node.(inclusivly)
            """
            name_nodes = set()
            if isinstance(root_node, gast.Name):
                if node.id in target_var_names:
                    name_nodes.add(root_node)
            for child_node in gast.walk(root_node):
                if isinstance(child_node, gast.Name):
                    if child_node.id in target_var_names:
                        name_nodes.add(child_node)

            return name_nodes

        vars_of_list_generator = set()
        target_vars_of_for_node = set()

        for name_node in loop_vars:
            if not isinstance(name_node, gast.Name):
                continue

            parent_node = self._get_parent_node(name_node)

            # NOTE: gast.For.target or gast.comprehension.target can be gast.Tuple.
            #  For examples:
            #   1) `for i, j in enumerate(x)` has two target vars: i and j
            #   2) `[x for x,y in array]` has two target vars: x and y
            if isinstance(parent_node, gast.Tuple):
                parent_node = self._get_parent_node(parent_node)

            # 1. Get vars only in gast.comprehension.
            # For examples:
            #  1) [x for x,y in array] -> x, x, y
            #  2) [f(x) for x in array] -> x
            #  3) [func(x, y) for x in array] -> x, x
            if isinstance(parent_node, gast.comprehension):
                # 1.1 target vars in list/set comprehensions
                target_node = parent_node.target
                if isinstance(target_node, gast.Tuple):
                    target_vars = target_node.elts
                else:
                    target_vars = [target_node]

                vars_of_list_generator = vars_of_list_generator | set(
                    target_vars
                )

                # 1.2 vars from target vars used in elt_node
                target_var_names = {var.id for var in target_vars}
                comp_node = self._get_parent_node(parent_node)
                elt_nodes = []
                if isinstance(comp_node, gast.ListComp):
                    elt_nodes.append(comp_node.elt)
                elif isinstance(comp_node, gast.DictComp):
                    elt_nodes.extend([comp_node.key, comp_node.value])

                for node in elt_nodes:
                    vars_of_list_generator |= filter_name_nodes_from(
                        node, target_var_names
                    )

            # 2. Get target vars or vars from target vars used in for-loop but the for-loop is
            #   1) not the "loop_node" itself
            #   2) not the ancestor of the "loop_node"
            #
            # For examples:
            #   for k in range(x):   # if it's this "loop_node", i or j both should be target vars.
            #      # do something
            #
            #   for i in range(a):   # if it's this "loop_node", k or j should be in target vars but i should not.
            #     for j in range(a): # if it's this "loop_node", k should be in target_vars but i or j should not.
            #       x = i+j
            elif isinstance(parent_node, gast.For):
                if parent_node is loop_node:
                    continue
                if self._is_ancestor_node(parent_node, loop_node):
                    continue
                # 2.1 target vars in gast.For node.
                target_node = parent_node.target
                if isinstance(target_node, gast.Tuple):
                    target_vars = target_node.elts
                else:
                    target_vars = [target_node]

                target_vars_of_for_node = target_vars_of_for_node | set(
                    target_vars
                )

        # 2.2 vars from target vars used in for-loop
        target_vars_name_strs = {var.id for var in target_vars_of_for_node}
        for var in loop_vars:
            if not isinstance(var, gast.Name):
                continue
            if (
                var.id in target_vars_name_strs
                and var not in self.condition_vars[loop_node]
            ):
                target_vars_of_for_node.add(var)

        removed_vars = target_vars_of_for_node | vars_of_list_generator

        # 3. Remove var type names which are stored in self.type_vars
        for var in loop_vars:
            if ast_to_source_code(var).strip() in self.type_vars:
                removed_vars.add(var)

        return loop_vars - removed_vars


class LoopTransformer(BaseTransformer):
    """
    This class transforms python while/for statement into Static Graph Ast
    """

    def __init__(self, root):
        self.root = root
        FunctionNameLivenessAnalysis(self.root)

    def transform(self):
        ForLoopTuplePreTransformer(self.root).transform()
        self.visit(self.root)

    def visit_While(self, node):
        self.generic_visit(node)
        new_stmts = self.get_while_stmt_nodes(node)
        return new_stmts

    def visit_For(self, node):
        self.generic_visit(node)
        new_stmts = self.get_for_stmt_nodes(node)
        return new_stmts

    def replace_stmt_list(self, body_list):
        if not isinstance(body_list, list):
            return

        i = 0
        while i < len(body_list):
            if isinstance(body_list[i], gast.While):
                new_stmts = self.get_while_stmt_nodes(body_list[i])
                body_list[i : i + 1] = new_stmts
                i += len(new_stmts)
            elif isinstance(body_list[i], gast.For):
                new_stmts = self.get_for_stmt_nodes(body_list[i])
                body_list[i : i + 1] = new_stmts
                i += len(new_stmts)
            else:
                i += 1

    def get_for_stmt_nodes(self, node):
        # TODO: consider for - else in python

        # 1. get key statements for different cases
        # NOTE 1: three key statements:
        #   1). init_stmts: list[node], prepare nodes of for loop, may not only one
        #   2). cond_stmt: node, condition node to judge whether continue loop
        #   3). body_stmts: list[node], updated loop body, sometimes we should change
        #       the original statement in body, not just append new statement
        #
        # NOTE 2: The following `for` statements will be transformed to `while` statements:
        #   1). for x in range(*)
        #   2). for x in iter_var
        #   3). for i, x in enumerate(*)

        current_for_node_parser = ForNodeVisitor(node)
        stmts_tuple = current_for_node_parser.parse()
        if stmts_tuple is None:
            return [node]
        init_stmts, cond_stmt, body_stmts = stmts_tuple
        # 2. get original loop vars
        loop_var_names, create_var_names = (
            node.pd_scope.modified_vars(),
            node.pd_scope.created_vars(),
        )
        push_pop_names = list(node.pd_scope.variadic_length_vars())
        # TODO: Remove the bunch of code?  We have the unique format `for A in B:`
        # NOTE: in 'for x in var' or 'for i, x in enumerate(var)' cases,
        # we need append new loop var & remove useless loop var
        #   1. for x in var -> x is no need
        #   2. for i, x in enumerate(var) -> x is no need
        if current_for_node_parser.is_for_iter():
            iter_var_name = current_for_node_parser.iter_var_name
            iter_idx_name = current_for_node_parser.iter_idx_name
            loop_var_names.add(iter_idx_name)
            if current_for_node_parser.enum_idx_name is not None:
                loop_var_names.add(current_for_node_parser.enum_idx_name)

        # 3. prepare result statement list
        new_stmts = []
        # Python can create variable in loop and use it out of loop, E.g.
        #
        # for x in range(10):
        #     y += x
        # print(x) # x = 10
        #
        # We don't need to create static variable for them, because
        # we do this in CreateUndefinedVarTransformer

        # create non-local statement for body and cond.
        nonlocal_names = list(loop_var_names | create_var_names)
        nonlocal_names.sort()
        # TODO(dev): Need a better way to deal this.
        if ARGS_NAME in nonlocal_names:
            nonlocal_names.remove(ARGS_NAME)

        nonlocal_stmt_node = create_nonlocal_stmt_nodes(nonlocal_names)

        # 4. append init statements
        new_stmts.extend(init_stmts)

        # 5. create & append condition function node
        condition_func_node = gast.FunctionDef(
            name=unique_name.generate(FOR_CONDITION_PREFIX),
            args=gast.arguments(
                args=[],
                posonlyargs=[],
                vararg=None,
                kwonlyargs=[],
                kw_defaults=None,
                kwarg=None,
                defaults=[],
            ),
            body=nonlocal_stmt_node + [gast.Return(value=cond_stmt)],
            decorator_list=[],
            returns=None,
            type_comment=None,
        )
        new_stmts.append(condition_func_node)

        # 6. create & append loop body function node
        # append return values for loop body
        body_func_node = gast.FunctionDef(
            name=unique_name.generate(FOR_BODY_PREFIX),
            args=gast.arguments(
                args=[],
                posonlyargs=[],
                vararg=None,
                kwonlyargs=[],
                kw_defaults=None,
                kwarg=None,
                defaults=[],
            ),
            body=nonlocal_stmt_node + body_stmts,
            decorator_list=[],
            returns=None,
            type_comment=None,
        )
        new_stmts.append(body_func_node)

        helper = GetterSetterHelper(None, None, nonlocal_names, push_pop_names)
        get_args_node = create_get_args_node(helper.union())
        set_args_node = create_set_args_node(helper.union())
        # 7. create & append while loop node
        while_loop_nodes = create_while_nodes(
            condition_func_node.name,
            body_func_node.name,
            nonlocal_names,
            push_pop_names,
            get_args_node.name,
            set_args_node.name,
        )
        new_stmts.extend([get_args_node, set_args_node])
        new_stmts.extend(while_loop_nodes)

        return new_stmts

    def get_while_stmt_nodes(self, node):
        loop_var_names, create_var_names = (
            node.pd_scope.modified_vars(),
            node.pd_scope.created_vars(),
        )
        push_pop_names = list(node.pd_scope.variadic_length_vars())
        new_stmts = []

        # create non-local statement for body and cond.
        nonlocal_names = list(loop_var_names | create_var_names)
        nonlocal_names.sort()
        # TODO(dev): Need a better way to deal this.
        if ARGS_NAME in nonlocal_names:
            nonlocal_names.remove(ARGS_NAME)

        nonlocal_stmt_node = create_nonlocal_stmt_nodes(nonlocal_names)

        # Python can create variable in loop and use it out of loop, E.g.
        #
        # while x < 10:
        #     x += 1
        #     y = x
        # z = y
        #
        # We don't need to create static variable for those variables, because
        # we do this in CreateUndefinedVarTransformer

        condition_func_node = gast.FunctionDef(
            name=unique_name.generate(WHILE_CONDITION_PREFIX),
            args=gast.arguments(
                args=[],
                posonlyargs=[],
                vararg=None,
                kwonlyargs=[],
                kw_defaults=None,
                kwarg=None,
                defaults=[],
            ),
            body=nonlocal_stmt_node + [gast.Return(value=node.test)],
            decorator_list=[],
            returns=None,
            type_comment=None,
        )

        new_stmts.append(condition_func_node)

        new_body = node.body
        body_func_node = gast.FunctionDef(
            name=unique_name.generate(WHILE_BODY_PREFIX),
            args=gast.arguments(
                args=[],
                posonlyargs=[],
                vararg=None,
                kwonlyargs=[],
                kw_defaults=None,
                kwarg=None,
                defaults=[],
            ),
            body=nonlocal_stmt_node + new_body,
            decorator_list=[],
            returns=None,
            type_comment=None,
        )
        new_stmts.append(body_func_node)

        helper = GetterSetterHelper(None, None, nonlocal_names, push_pop_names)
        get_args_node = create_get_args_node(helper.union())
        set_args_node = create_set_args_node(helper.union())

        while_loop_nodes = create_while_nodes(
            condition_func_node.name,
            body_func_node.name,
            nonlocal_names,
            push_pop_names,
            get_args_node.name,
            set_args_node.name,
        )
        new_stmts.extend([get_args_node, set_args_node])
        new_stmts.extend(while_loop_nodes)
        return new_stmts
