# Copyright 2023 Turing Inc. Team. All rights reserved.
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

"""PyTorch GIT Wizard model."""

import copy
from typing import List, Optional, Tuple, Union

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn import CrossEntropyLoss
from transformers import (
    CLIPVisionConfig,
    CLIPVisionModel,
    GPTBigCodeConfig,
    GPTBigCodeForCausalLM,
    GPTBigCodeModel,
)
from transformers.modeling_outputs import (
    BaseModelOutputWithPastAndCrossAttentions,
    BaseModelOutputWithPooling,
    CausalLMOutputWithPast,
)
from transformers.models.git.modeling_git import GitProjection


class GitGPTBigCodeConfig(GPTBigCodeConfig):
    model_type = "git_llama"

    def __init__(
        self,
        **kwargs,
    ):
        super().__init__(**kwargs)
        if hasattr(self, "vision_model_name"):
            self.set_vision_configs(self.num_image_with_embedding, self.vision_model_name)
        else:
            self.vision_config = CLIPVisionConfig()
            self.num_image_with_embedding = None

    def set_vision_configs(
        self,
        num_image_with_embedding: int = 1,
        vision_model_name: Union[str, None] = None,
    ):
        self.num_image_with_embedding = (
            None if num_image_with_embedding == 1 else num_image_with_embedding
        )
        self.vision_model_name = vision_model_name
        self.vision_config = CLIPVisionConfig.from_pretrained(vision_model_name)

    def to_dict(self):
        """
        Serializes this instance to a Python dictionary. Override the default [`~PretrainedConfig.to_dict`]. Returns:
            `Dict[str, any]`: Dictionary of all the attributes that make up this configuration instance,
        """
        output = copy.deepcopy(self.__dict__)
        output["vision_config"] = self.vision_config.to_dict()
        output["model_type"] = self.__class__.model_type
        return output


# Copied from transformers.models.bart.modeling_bart._expand_mask
def _expand_mask(mask: torch.Tensor, dtype: torch.dtype, tgt_len: Optional[int] = None):
    """
    Expands attention_mask from `[bsz, seq_len]` to `[bsz, 1, tgt_seq_len, src_seq_len]`.
    """
    bsz, src_len = mask.size()
    tgt_len = tgt_len if tgt_len is not None else src_len

    expanded_mask = mask[:, None, None, :].expand(bsz, 1, tgt_len, src_len).to(dtype)

    inverted_mask = 1.0 - expanded_mask

    return inverted_mask.masked_fill(inverted_mask.to(torch.bool), torch.finfo(dtype).min)


class GitGPTBigCodeModel(GPTBigCodeModel):
    config_class = GitGPTBigCodeConfig

    def __init__(self, config: GitGPTBigCodeConfig):
        super(GitGPTBigCodeModel, self).__init__(config)

        # Git modules
        self.image_encoder = CLIPVisionModel.from_pretrained(config.vision_model_name)
        self.visual_projection = GitProjection(config)

        if config.num_image_with_embedding is not None:
            self.img_temporal_embedding = nn.ParameterList(
                nn.Parameter(torch.zeros(1, 1, config.vision_config.hidden_size))
                for _ in range(config.num_image_with_embedding)
            )

        self.image_patch_tokens = int(
            (config.vision_config.image_size / config.vision_config.patch_size) ** 2 + 1
        )
        if config.num_image_with_embedding is not None:
            self.image_patch_tokens *= config.num_image_with_embedding

        # Initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        return self.decoder.embed_tokens

    def set_input_embeddings(self, value):
        self.decoder.embed_tokens = value

    def _prune_heads(self, heads_to_prune):
        """
        Prunes heads of the model. heads_to_prune: dict of {layer_num: list of heads to prune in this layer} See base
        class PreTrainedModel
        """
        for layer, heads in heads_to_prune.items():
            self.encoder.layer[layer].attention.prune_heads(heads)

    def _generate_future_mask(
        self, size: int, dtype: torch.dtype, device: torch.device
    ) -> torch.Tensor:
        # Default mask is for forward direction. Flip for backward direction.
        mask = torch.tril(torch.ones(size, size, device=device, dtype=dtype), diagonal=0)
        # mask = mask.masked_fill(mask == 1, float("-inf"))
        return mask

    def create_attention_mask(
        self,
        tgt,
        memory,
        tgt_mask,
        past_key_values_length,
        memory_key_padding_mask=None,
    ):
        num_tgt = tgt.shape[1]
        num_memory = memory.shape[1]
        device = tgt.device
        dtype = tgt.dtype
        top_left = torch.ones((num_memory, num_memory), device=device, dtype=dtype)
        top_right = torch.full(
            (num_memory, num_tgt + past_key_values_length),
            float(0),
            device=tgt.device,
            dtype=dtype,
        )
        bottom_left = torch.ones(
            (num_tgt, num_memory),
            dtype=dtype,
            device=tgt_mask.device,
        )

        if past_key_values_length > 0:
            tgt_mask = torch.ones(
                (tgt_mask.shape[0], tgt_mask.shape[0] + past_key_values_length),
                dtype=dtype,
                device=tgt_mask.device,
            )

        left = torch.cat((top_left, bottom_left), dim=0)
        right = torch.cat((top_right, tgt_mask.to(dtype)), dim=0)

        full_attention_mask = torch.cat((left, right), dim=1)[None, :]

        if memory_key_padding_mask is None:
            memory_key_padding_mask = torch.full(
                (memory.shape[0], memory.shape[1]), fill_value=False, device=device
            )
        # if it is False, it means valid. That is, it is not a padding
        if memory_key_padding_mask.dtype != torch.bool:
            raise ValueError("Memory key padding mask must be a boolean tensor.")
        zero_negative_infinity = torch.zeros_like(memory_key_padding_mask, dtype=tgt.dtype)
        zero_negative_infinity[memory_key_padding_mask] = float(-1)
        full_attention_mask = full_attention_mask.expand(
            (
                memory_key_padding_mask.shape[0],
                num_memory + num_tgt,
                num_memory + past_key_values_length + num_tgt,
            )
        )
        full_attention_mask = full_attention_mask.clone()
        origin_left = full_attention_mask[:, :, :num_memory]
        update = zero_negative_infinity[:, None, :]
        full_attention_mask[:, :, :num_memory] = origin_left + update

        # add axis for multi-head
        full_attention_mask = full_attention_mask[:, None, :, :]

        return full_attention_mask

    def forward(
        self,
        input_ids: Optional[torch.Tensor] = None,
        past_key_values: Optional[List[torch.Tensor]] = None,
        attention_mask: Optional[torch.Tensor] = None,
        token_type_ids: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.Tensor] = None,
        pixel_values: Optional[torch.Tensor] = None,
        head_mask: Optional[torch.Tensor] = None,
        inputs_embeds: Optional[torch.Tensor] = None,
        encoder_hidden_states: Optional[torch.Tensor] = None,
        encoder_attention_mask: Optional[torch.Tensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, BaseModelOutputWithPastAndCrossAttentions]:
        output_attentions = (
            output_attentions if output_attentions is not None else self.config.output_attentions
        )
        output_hidden_states = (
            output_hidden_states
            if output_hidden_states is not None
            else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if input_ids is not None and inputs_embeds is not None:
            raise ValueError(
                "You cannot specify both input_ids and inputs_embeds at the same time"
            )
        elif input_ids is not None:
            self.warn_if_padding_and_no_attention_mask(input_ids, attention_mask)
            input_shape = input_ids.size()
            input_ids = input_ids.view(-1, input_shape[-1])
            batch_size = input_ids.shape[0]
        elif inputs_embeds is not None:
            input_shape = inputs_embeds.size()[:-1]
            batch_size = inputs_embeds.shape[0]
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        if batch_size <= 0:
            raise ValueError("batch_size has to be defined and > 0")

        device = input_ids.device if input_ids is not None else inputs_embeds.device

        if token_type_ids is not None:
            token_type_ids = token_type_ids.view(-1, input_shape[-1])
        if position_ids is not None:
            position_ids = position_ids.view(-1, input_shape[-1])

        seq_length_with_past = input_shape[-1]

        if past_key_values is None:
            past_length = 0
        else:
            past_length = past_key_values[0].size(-2)
            seq_length_with_past = seq_length_with_past + past_length

        # GIT Vision Encoder part
        projected_visual_features = None
        if pixel_values is not None and past_key_values is None:
            if pixel_values.ndim == 4:
                # here we assume pixel_values is of shape (batch_size, num_channels, height, width)
                visual_features = self.image_encoder(pixel_values).last_hidden_state

            elif pixel_values.ndim == 5:
                # here we assume pixel_values is of shape (batch_size, num_frames, num_channels, height, width)
                visual_features = []
                for frame_idx in range(pixel_values.shape[1]):
                    visual_features_frame = self.image_encoder(
                        pixel_values[:, frame_idx, :, :]
                    ).last_hidden_state
                    visual_features_frame += self.img_temporal_embedding[frame_idx]
                    visual_features.append(visual_features_frame)

                # finally, concatenate all features along sequence dimension
                visual_features = torch.cat(visual_features, dim=1)
            else:
                raise ValueError("pixel_values must be of rank 4 or 5")

            projected_visual_features = self.visual_projection(visual_features)

        if inputs_embeds is None:
            inputs_embeds = self.wte(input_ids)

        # if attention_mask is not None and len(attention_mask.shape) == 2 and position_ids is None:
        #     # create position_ids on the fly for batch generation
        #     position_ids = attention_mask.long().cumsum(-1) - 1
        #     position_ids.masked_fill_(attention_mask == 0, 1)
        #     if past_length > 0:
        #         position_ids = position_ids[:, past_length : input_shape[-1] + past_length :]
        # elif position_ids is None:
        #     position_ids = torch.arange(past_length, input_shape[-1] + past_length, dtype=torch.long, device=device)
        #     position_ids = position_ids.unsqueeze(0).view(-1, input_shape[-1])

        # Self-attention mask.
        query_length = input_shape[-1]
        key_length = past_length + query_length
        # self_attention_mask = self.bias[None, key_length - query_length : key_length, :key_length]

        # if attention_mask is not None:
        #     self_attention_mask = self_attention_mask * attention_mask.view(batch_size, 1, -1).to(
        #         dtype=torch.bool, device=self_attention_mask.device
        #     )

        # # MQA models: (batch_size, query_length, n_heads, key_length)
        # MQAだと壊れる？
        # # MHA models: (batch_size, n_heads, query_length, key_length)
        # attention_mask = self_attention_mask.unsqueeze(2 if self.multi_query else 1)

        # If a 2D or 3D attention mask is provided for the cross-attention
        # we need to make broadcastable to [batch_size, num_heads, seq_length, seq_length]
        if (
            self.config.add_cross_attention
            and encoder_hidden_states is not None
            and encoder_attention_mask is not None
        ):
            if encoder_attention_mask.dim() == 2:
                encoder_attention_mask.unsqueeze(1)
            assert encoder_attention_mask.dim() == 3
            encoder_attention_mask = encoder_attention_mask.bool().unsqueeze(
                2 if self.multi_query else 1
            )
        else:
            encoder_attention_mask = None

        if projected_visual_features is None:
            projected_visual_features = torch.zeros(
                (inputs_embeds.shape[0], 0, inputs_embeds.shape[2]),
                dtype=inputs_embeds.dtype,
                device=inputs_embeds.device,
            )

        # Repeat visual features to match embedding batch size.
        projected_visual_features = projected_visual_features.repeat(
            inputs_embeds.size(0) // projected_visual_features.size(0), 1, 1
        )

        hidden_states = torch.cat((projected_visual_features, inputs_embeds), dim=1)

        if position_ids is None:
            device = input_ids.device if input_ids is not None else inputs_embeds.device
            position_ids = torch.arange(
                past_length,
                query_length + projected_visual_features.shape[1] + past_length,
                dtype=torch.long,
                device=device,
            )
            position_ids = position_ids.unsqueeze(0).view(
                -1, query_length + projected_visual_features.shape[1]
            )
        else:
            position_ids = position_ids.view(
                -1, query_length + projected_visual_features.shape[1]
            ).long()

        # Prepare head mask if needed
        # 1.0 in head_mask indicate we keep the head
        # attention_probs has shape bsz x n_heads x N x N
        # head_mask has shape n_layer x batch x n_heads x N x N
        head_mask = self.get_head_mask(head_mask, self.config.n_layer)

        position_embeds = self.wpe(position_ids)
        hidden_states = hidden_states + position_embeds

        if token_type_ids is not None:
            token_type_embeds = self.wte(token_type_ids)
            hidden_states = hidden_states + token_type_embeds

        hidden_states = self.drop(hidden_states)

        # By default, an additive causal mask is created
        # for masking the future (one direction).
        tgt_mask = self._generate_future_mask(
            query_length, inputs_embeds.dtype, inputs_embeds.device
        )

        combined_attention_mask = self.create_attention_mask(
            tgt=inputs_embeds,
            memory=projected_visual_features,
            tgt_mask=tgt_mask,
            past_key_values_length=past_length,
        ).bool()

        if attention_mask is not None:
            # if the user provides an attention mask, we add it to the default one
            # [bsz, seq_len] -> [bsz, 1, tgt_seq_len, src_seq_len]
            expanded_attn_mask = (
                _expand_mask(attention_mask, inputs_embeds.dtype, tgt_len=input_shape[-1])
                .bool()
                .to(inputs_embeds.device)
            )
            if past_length > 0:
                expanded_attn_mask = expanded_attn_mask[:, :, -past_length:, :]
            else:
                combined_attention_mask[
                    :, :, -input_shape[1] :, -input_shape[1] :
                ] = torch.logical_and(
                    combined_attention_mask[:, :, -input_shape[1] :, -input_shape[1] :],
                    expanded_attn_mask,
                )

        if self.multi_query:
            combined_attention_mask = combined_attention_mask.transpose(1, 2)

        if past_key_values is None:
            past_key_values = tuple([None] * len(self.h))

        output_shape = hidden_states.shape  # + (hidden_states.size(-1),)

        presents = [] if use_cache else None
        all_self_attentions = () if output_attentions else None
        all_cross_attentions = (
            () if output_attentions and self.config.add_cross_attention else None
        )
        all_hidden_states = () if output_hidden_states else None
        for i, (block, layer_past) in enumerate(zip(self.h, past_key_values)):
            if output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states,)

            if self.gradient_checkpointing and self.training:

                def create_custom_forward(module):
                    def custom_forward(*inputs):
                        # None for past_key_value
                        return module(*inputs, use_cache, output_attentions)

                    return custom_forward

                outputs = torch.utils.checkpoint.checkpoint(
                    create_custom_forward(block),
                    hidden_states,
                    None,
                    combined_attention_mask,
                    head_mask[i],
                    encoder_hidden_states,
                    encoder_attention_mask,
                )
            else:
                outputs = block(
                    hidden_states,
                    layer_past=layer_past,
                    attention_mask=combined_attention_mask,
                    head_mask=head_mask[i],
                    encoder_hidden_states=encoder_hidden_states,
                    encoder_attention_mask=encoder_attention_mask,
                    use_cache=use_cache,
                    output_attentions=output_attentions,
                )

            hidden_states = outputs[0]
            if use_cache:
                presents.append(outputs[1])

            if output_attentions:
                all_self_attentions = all_self_attentions + (outputs[2 if use_cache else 1],)
                if self.config.add_cross_attention:
                    all_cross_attentions = all_cross_attentions + (outputs[3 if use_cache else 2],)

        hidden_states = self.ln_f(hidden_states)

        hidden_states = hidden_states.view(output_shape)
        # Add last hidden state
        if output_hidden_states:
            all_hidden_states = all_hidden_states + (hidden_states,)

        if not return_dict:
            return tuple(
                v
                for v in [
                    hidden_states,
                    presents,
                    all_hidden_states,
                    all_self_attentions,
                    all_cross_attentions,
                ]
                if v is not None
            )

        return BaseModelOutputWithPastAndCrossAttentions(
            last_hidden_state=hidden_states,
            past_key_values=presents,
            hidden_states=all_hidden_states,
            attentions=all_self_attentions,
            cross_attentions=all_cross_attentions,
        )


class GitGPTBigCodeForCausalLM(GPTBigCodeForCausalLM):
    config_class = GitGPTBigCodeConfig

    def __init__(
        self,
        config,
    ):
        super(GitGPTBigCodeForCausalLM, self).__init__(config)
        self.model = GitGPTBigCodeModel(config)

        # Initialize weights and apply final processing
        self.post_init()

    def forward(
        self,
        input_ids: Optional[torch.Tensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.Tensor] = None,
        pixel_values: Optional[torch.Tensor] = None,
        inputs_embeds: Optional[torch.Tensor] = None,
        labels: Optional[torch.Tensor] = None,
        past_key_values: Optional[List[torch.Tensor]] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple[torch.Tensor], CausalLMOutputWithPast]:
        r"""
        labels (`torch.LongTensor` of shape `(batch_size, sequence_length)`, *optional*):
            Labels for computing the left-to-right language modeling loss (next word prediction). Indices should be in
            `[-100, 0, ..., config.vocab_size]` (see `input_ids` docstring) Tokens with indices set to `-100` are
            ignored (masked), the loss is only computed for the tokens with labels n `[0, ..., config.vocab_size]`
        past_key_values (`tuple(tuple(torch.FloatTensor))` of length `config.n_layers` with each tuple having 4 tensors of shape `(batch_size, num_heads, sequence_length - 1, embed_size_per_head)`):
            Contains precomputed key and value hidden states of the attention blocks. Can be used to speed up decoding.

            If `past_key_values` are used, the user can optionally input only the last `decoder_input_ids` (those that
            don't have their past key value states given to this model) of shape `(batch_size, 1)` instead of all
            `decoder_input_ids` of shape `(batch_size, sequence_length)`.
        use_cache (`bool`, *optional*):
            If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding (see
            `past_key_values`).

        Returns:
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict
        if labels is not None:
            use_cache = False

        outputs = self.model(
            input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            pixel_values=pixel_values,
            inputs_embeds=inputs_embeds,
            past_key_values=past_key_values,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        sequence_output = outputs[0]
        logits = self.lm_head(sequence_output)

        loss = None
        if labels is not None:
            # we are doing next-token prediction; shift prediction scores and input ids by one
            num_image_tokens = self.model.image_patch_tokens
            shifted_logits = logits[:, num_image_tokens:-1, :].contiguous()
            labels = labels[:, 1:].contiguous()
            loss_fct = CrossEntropyLoss()
            loss = loss_fct(shifted_logits.view(-1, self.config.vocab_size), labels.view(-1))

        if not return_dict:
            output = (logits,) + outputs[1:]
            return ((loss,) + output) if loss is not None else output

        return CausalLMOutputWithPast(
            loss=loss,
            logits=logits,
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )

    def prepare_inputs_for_generation(
        self,
        input_ids,
        past_key_values=None,
        attention_mask=None,
        use_cache=None,
        **kwargs,
    ):
        # cut decoder_input_ids if past_key_values is used
        if past_key_values is not None:
            input_ids = input_ids[:, -1:]

        # if model is used as a decoder in encoder-decoder model, the decoder attention mask is created on the fly
        input_shape = input_ids.shape
        if attention_mask is None:
            attention_mask = input_ids.new_ones(input_shape)

        return {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "pixel_values": kwargs.get("pixel_values", None),
            "past_key_values": past_key_values,
            "use_cache": use_cache,
        }

    def _reorder_cache(self, past_key_values, beam_idx):
        reordered_past = ()
        for layer_past in past_key_values:
            reordered_past += (
                tuple(past_state.index_select(0, beam_idx) for past_state in layer_past),
            )
        return reordered_past
