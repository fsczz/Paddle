/* Copyright (c) 2016 PaddlePaddle Authors. All Rights Reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License. */

#pragma once
#include <functional>
#include <map>
#include <memory>
#include <string>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "paddle/fluid/imperative/type_defs.h"

#include "paddle/phi/common/scalar.h"
#include "paddle/phi/core/infermeta_utils.h"
#include "paddle/pir/core/block.h"
#include "paddle/pir/core/value.h"
#include "paddle/utils/blank.h"
#include "paddle/utils/small_vector.h"
#include "paddle/utils/variant.h"

namespace paddle {
namespace framework {
class OperatorBase;
class OpDesc;
class InferShapeContext;
class InferVarTypeContext;
class VarDesc;
class BlockDesc;
class Variable;
class InferNoNeedBufferVarsFN;

// TODO(panyx0718): Replace vector with something like gtl::Vector.
using VariableNameMap = std::map<std::string, std::vector<std::string>>;
using VariableValueMap = std::map<std::string, std::vector<Variable*>>;

using Attribute = paddle::variant<paddle::blank,
                                  int,
                                  float,
                                  std::string,
                                  std::vector<int>,
                                  std::vector<float>,
                                  std::vector<std::string>,
                                  bool,
                                  std::vector<bool>,
                                  BlockDesc*,
                                  int64_t,
                                  std::vector<BlockDesc*>,
                                  std::vector<int64_t>,
                                  std::vector<double>,
                                  VarDesc*,
                                  std::vector<VarDesc*>,
                                  double,
                                  paddle::experimental::Scalar,
                                  std::vector<paddle::experimental::Scalar>,
                                  ::pir::Block*,
                                  std::vector<::pir::Value>>;
using AttributeMap = std::unordered_map<std::string, Attribute>;

using OpCreator =
    std::function<OperatorBase*(const std::string& /*type*/,
                                const VariableNameMap& /*inputs*/,
                                const VariableNameMap& /*outputs*/,
                                const AttributeMap& /*attrs*/)>;

using GradOpMakerFN = std::function<std::vector<std::unique_ptr<OpDesc>>(
    const OpDesc&,
    const std::unordered_set<std::string>& /*no_grad_set*/,
    std::unordered_map<std::string, std::string>* /*grad_to_var*/,
    const std::vector<BlockDesc*>& grad_block)>;

using CompositeGradOpMakerFN =
    std::function<std::vector<std::unique_ptr<OpDesc>>(
        const OpDesc&,
        const std::unordered_set<std::string>& /*no_grad_set*/,
        std::unordered_map<std::string, std::string>* /*grad_to_var*/,
        const BlockDesc*,
        const std::vector<BlockDesc*>& grad_block)>;

using DygraphGradOpMakerFN =
    std::function<std::shared_ptr<imperative::GradOpNode>(
        const std::string& /*op_type*/,
        const imperative::NameVarBaseMap& /*var_base_map_in*/,
        const imperative::NameVarBaseMap& /*var_base_map_out*/,
        const framework::AttributeMap& /*attributes*/,
        const framework::AttributeMap& /*default attributes*/,
        const std::map<std::string, std::string>& /*inplace_map*/)>;

using InferVarTypeFN =
    std::function<void(framework::InferVarTypeContext* /*context*/)>;

using InferShapeFN = std::function<void(InferShapeContext*)>;
using InferMetaFN = std::function<void(phi::InferMetaContext*)>;

using InplacePair = std::unordered_map<std::string, std::string>;
using InferInplaceOpFN = std::function<InplacePair(bool /*use_cuda*/)>;

}  // namespace framework
}  // namespace paddle
